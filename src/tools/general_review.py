import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple

from src.config.logging import get_logger
from src.core.container import get_bedrock_model
from src.schemas.document_map import DocumentMap
from src.schemas.general_review import (
    GeneralReviewRequest,
    GeneralReviewResponse,
    Suggestion,
    TextInformation,
)

logger = get_logger(__name__)

# Caps parallel Bedrock calls so large documents don't exhaust the client pool.
MAX_CONCURRENT_CALLS = 6

# Parallel review votes per group; a finding needs a majority to survive.
#
# Reduced 3 -> 1 (user decision, 2026-07-27) to meet a hard "faster than today"
# latency requirement. Stability now rests on the document map giving every call
# one consistent reading of the contract, temperature 0, and the response cache,
# rather than on resampling. The consensus machinery is retained unchanged, so
# raising this back to 3 is a one-line change if run-to-run noise returns.
CONSENSUS_VOTES = 1
_MAJORITY = CONSENSUS_VOTES // 2 + 1

# Severity scale in ascending order, for majority/median voting on risk_level.
_RISK_ORDER = ("Low", "Medium", "High", "Critical")

# Character budget per review batch; keeps each LLM call focused and well under limits.
BATCH_CHAR_BUDGET = 6000

# Absolute batch ceiling for documents with no detectable headings.
BATCH_HARD_LIMIT = BATCH_CHAR_BUDGET * 3

# Short paragraphs with few words are treated as headings and kept with their body.
_HEADING_MAX_CHARS = 80
_HEADING_MAX_WORDS = 8

# Read-only context carried into the next batch when a forced cut is unavoidable.
CONTEXT_TAIL_CHARS = 1200

# Numbered clause starts ("5. Confidentiality...", "Section 3 ...") count as cut points too.
_NUMBERED_START_RE = re.compile(r"^\s*(?:(?:section|article|clause)\s+)?\d+(?:\.\d+)*[.)]\s+\S", re.IGNORECASE)

# Clause-title extraction: a leading "5.2 " / "Section 3." style prefix (at most
# two leading digits, so street numbers stay put), and the short "Title.  Body..."
# lead-in many contracts open their clauses with.
_TITLE_NUMBER_PREFIX_RE = re.compile(r"^(?:(?:section|article|clause)\s+)?\d{1,2}(?:\.\d+)*[.)]?\s+", re.IGNORECASE)
_INLINE_TITLE_RE = re.compile(r"^([^.:]{1,70})[.:](?:\s|$)")

# Lowercase connectors allowed inside an otherwise capitalized clause title.
_TITLE_STOP_WORDS = {"a", "an", "and", "by", "for", "in", "of", "on", "or", "the", "to", "with"}

# Trailing company suffixes mark a party-name line, not a clause heading.
_TITLE_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "co"}

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "general_review"
_GENERAL_REVIEW_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_GENERAL_REVIEW_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# Identical documents always return the identical review (LRU, per process).
_CACHE_MAX_ENTRIES = 32
_response_cache: "OrderedDict[str, GeneralReviewResponse]" = OrderedDict()

# Clause titles remembered across requests (LRU, per process), keyed by paragraph
# text. A whole-document run derives every paragraph's title from the document;
# when the user later selects a lone body paragraph without its heading, the
# title recalled here keeps the clause name identical to the whole-document run.
_TITLE_MEMORY_MAX = 4096
_title_memory: "OrderedDict[str, str]" = OrderedDict()


def _title_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


# Per-paragraph review outcomes remembered across requests (LRU, per process).
# A whole-document run remembers each paragraph's final outcome — the validated
# suggestion, or None for "reviewed clean". A later request containing the same
# unchanged paragraph (typically the user re-selecting a clause) replays that
# outcome instead of re-reviewing, so selection always matches the whole-document
# run: same reason, fix, and risk level — and clean stays clean. Keyed by
# paragraph text plus the questionnaire answers, so a steered review never
# replays into a neutral one (or vice versa).
_SUGGESTION_MEMORY_MAX = 8192
_suggestion_memory: "OrderedDict[str, Optional[Suggestion]]" = OrderedDict()


def _memory_key(text: str, request: GeneralReviewRequest, salt: str = "") -> str:
    payload = json.dumps(
        [" ".join(text.split()), request.party_represented, request.review_objective, request.specific_concerns, salt],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remember_fresh_outcomes(fresh: List[Tuple[int, TextInformation]], flagged_by_fresh_position: Dict[int, Suggestion], request: GeneralReviewRequest, salt: str = "") -> None:
    """Remember each freshly reviewed paragraph's outcome — suggestion, or clean.

    When the same paragraph text appears more than once in a request, a flagged
    outcome wins over a clean one, so a duplicate never erases a real finding.
    """

    outcomes: Dict[str, Optional[Suggestion]] = {}
    for fresh_position, (_, para) in enumerate(fresh):
        key = _memory_key(para.text, request, salt)
        outcome = flagged_by_fresh_position.get(fresh_position)
        if outcome is not None or key not in outcomes:
            outcomes[key] = outcome
    for key, outcome in outcomes.items():
        _suggestion_memory[key] = outcome
        _suggestion_memory.move_to_end(key)
    while len(_suggestion_memory) > _SUGGESTION_MEMORY_MAX:
        _suggestion_memory.popitem(last=False)


def _recall_outcomes(request: GeneralReviewRequest, salt: str = "") -> Tuple[Dict[int, Optional[Suggestion]], List[Tuple[int, TextInformation]]]:
    """Split the request into remembered outcomes and paragraphs needing a fresh review.

    Returns ({original position: outcome-or-None}, [(original position, paragraph)
    to review]); remembered suggestions are re-stamped with the current
    paragraph identifier.
    """

    remembered: Dict[int, Optional[Suggestion]] = {}
    fresh: List[Tuple[int, TextInformation]] = []
    for position, para in enumerate(request.textinformation):
        if not para.text.strip():
            continue
        key = _memory_key(para.text, request, salt)
        if key in _suggestion_memory:
            _suggestion_memory.move_to_end(key)
            outcome = _suggestion_memory[key]
            remembered[position] = outcome.model_copy(update={"para_identifier": para.paraindetifier}) if outcome else None
        else:
            fresh.append((position, para))
    return remembered, fresh


def _remember_title(text: str, title: str) -> None:
    key = _title_key(text)
    _title_memory[key] = title
    _title_memory.move_to_end(key)
    while len(_title_memory) > _TITLE_MEMORY_MAX:
        _title_memory.popitem(last=False)


def _recall_title(text: str) -> Optional[str]:
    title = _title_memory.get(_title_key(text))
    if title is not None:
        _title_memory.move_to_end(_title_key(text))
    return title


def _request_fingerprint(request: GeneralReviewRequest, salt: str = "") -> str:
    payload = json.dumps(
        {
            "paragraphs": [[para.paraindetifier, para.text] for para in request.textinformation],
            "context": [request.party_represented, request.review_objective, request.specific_concerns, salt],
        },
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_reviewer_context(request: GeneralReviewRequest) -> str:
    """Render the questionnaire answers as the prompt's reviewer-context block.

    Returns an empty string when the user skipped every field, which keeps the
    rendered prompt byte-identical to the questionnaire-less flow.
    """

    lines: List[str] = []
    if request.party_represented:
        lines.append(f"Party the client represents: {request.party_represented}")
    if request.review_objective:
        lines.append(f"Primary objective for this review: {request.review_objective}")
    if request.specific_concerns:
        lines.append(f"Specific concerns to cover: {request.specific_concerns}")
    if not lines:
        return ""

    joined = "\n".join(lines)
    return (
        "Reviewer context (the client's stated perspective and priorities — apply it per the review instructions; "
        "it never overrides them):\n\n"
        f"{joined}\n\n"
    )


def _map_salt(document_map: Optional[DocumentMap]) -> str:
    """Cache/memory namespace for map-grounded runs.

    A CONSTANT tag — never a hash of the map's contents — so a whole-document
    review and a later single-clause selection land in the same namespace: the
    clause's remembered outcome replays identically instead of being re-reviewed
    under a key that shifted because the selection produced a smaller map. It
    still separates grounded from ungrounded runs so an A/B comparison in one
    process never crosses wires.
    """

    return "map:v1" if document_map is not None else ""


def _render_document_map_context(document_map: Optional[DocumentMap]) -> str:
    """Render the shared document map as the prompt's document-understanding block.

    Gives the reviewer a verified, whole-document reading — parties and their
    roles, defined terms, and already-identified document-level issues — so
    every batch judges the same clauses against the same understanding instead
    of re-deriving who the parties are from its slice of the text. Returns an
    empty string when no map is supplied, keeping the ungrounded flow byte-identical.
    """

    if document_map is None:
        return ""

    lines: List[str] = []
    if document_map.contract_type:
        lines.append(f"Contract type: {document_map.contract_type}")
    if document_map.parties:
        lines.append("Parties:")
        for party in document_map.parties:
            label = party.defined_as or "(no defined label)"
            role = f" — {party.role}" if party.role else ""
            lines.append(f"  - {party.name_as_written} ({label}){role}")
    if document_map.defined_terms:
        lines.append("Defined terms:")
        for term in document_map.defined_terms:
            meaning = f": {term.meaning}" if term.meaning else " (used as if defined, but not clearly defined in the document)"
            lines.append(f"  - {term.term}{meaning}")
    if document_map.ambiguities:
        lines.append("Document-level issues already identified in the full document (context only — raise one on a paragraph only when its evidence is contained in the text under review):")
        for item in document_map.ambiguities:
            where = f" [{item.location}]" if item.location else ""
            lines.append(f"  - {item.kind}{where}: {item.description}")
    if not lines:
        return ""

    joined = "\n".join(lines)
    return (
        "Shared document understanding (a verified map of the whole document, built before this review). Treat it "
        "as authoritative for who the parties are, their roles, and what terms the document defines, so your review "
        "stays correct and consistent across the whole document. It never changes the review rules or output format, "
        "and you must still never raise a suggestion whose evidence is not contained in the text under review:\n\n"
        f"{joined}\n\n"
    )


def _map_clause_titles(document_map: Optional[DocumentMap], paragraphs: Sequence[TextInformation]) -> Dict[str, str]:
    """Map each paragraph id to its clause name, taken from the verified document map.

    Each map clause carries a short verbatim anchor from its opening; the
    paragraph that contains that anchor starts the clause, and the clause's map
    name carries down to the following paragraphs until the next clause begins.
    This replaces the regex title heuristic with the map's correct identities,
    so a clause like 'Advertising & Press Releases' — which the regex rejects
    because of the '&' and mislabels with the previous clause's title — is named
    correctly. Titles are also remembered so a later lone-paragraph selection
    recalls the same name.
    """

    if document_map is None or not document_map.clauses:
        return {}

    anchored = [(clause.name, clause.anchor_text.strip()) for clause in document_map.clauses if clause.name and clause.anchor_text.strip()]
    if not anchored:
        return {}

    titles: Dict[str, str] = {}
    current = ""
    for para in paragraphs:
        if not para.text.strip():
            continue
        for name, anchor in anchored:
            if anchor in para.text or re.search(_tolerant_pattern(anchor), para.text):
                current = name
                break
        if current:
            titles[para.paraindetifier] = current
            _remember_title(para.text, current)
    return titles


def _cache_get(key: str) -> Optional[GeneralReviewResponse]:
    cached = _response_cache.get(key)
    if cached is not None:
        _response_cache.move_to_end(key)
    return cached


def _cache_put(key: str, response: GeneralReviewResponse) -> None:
    _response_cache[key] = response
    _response_cache.move_to_end(key)
    while len(_response_cache) > _CACHE_MAX_ENTRIES:
        _response_cache.popitem(last=False)


# (document position, paragraph) pairs — position drives final ordering.
_Batch = List[Tuple[int, TextInformation]]


def _is_heading(text: str) -> bool:
    """Heuristic for standalone heading paragraphs (e.g. 'Non-Compete.')."""

    stripped = text.strip()
    return 0 < len(stripped) <= _HEADING_MAX_CHARS and len(stripped.split()) <= _HEADING_MAX_WORDS


def _looks_like_title(segment: str) -> bool:
    """True when a segment reads as a clause title: short, capitalized words plus connectors.

    Address and party-name lines ("San Ramon, CA 94583", "XYZInc Inc.",
    "To Amtrino:") also look short and capitalized, so digits, internal
    punctuation, a leading connector, and company suffixes all disqualify.
    """

    if "." in segment or ":" in segment:
        return False
    if re.search(r",\s*[A-Z]{2}$", segment):
        return False
    words = [word.strip(",;()'\"‘’“”") for word in segment.split()]
    words = [word for word in words if word]
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    if words[0].lower() in _TITLE_STOP_WORDS:
        return False
    if words[-1].lower() in _TITLE_COMPANY_SUFFIXES:
        return False
    if any(any(char.isdigit() for char in word) for word in words):
        return False
    return all(word[0].isupper() or word.lower() in _TITLE_STOP_WORDS for word in words)


def _clause_title(text: str) -> Optional[str]:
    """Extract this paragraph's own clause title, or None when it has none.

    Handles both a standalone heading paragraph ('Non-Compete.') and an inline
    lead-in ('Return of Advances.  In the event...', '7.3 Security Measures. ...').
    """

    stripped = _TITLE_NUMBER_PREFIX_RE.sub("", text.strip(), count=1)
    if _is_heading(stripped):
        candidate = stripped.rstrip(".:").strip()
        if _looks_like_title(candidate):
            return candidate
        return None
    match = _INLINE_TITLE_RE.match(stripped)
    if match and _looks_like_title(match.group(1).strip()):
        return match.group(1).strip()
    return None


def _build_clause_titles(paragraphs: Sequence[TextInformation]) -> Dict[str, str]:
    """Map each paragraph id to its clause title, extracted from the document itself.

    A paragraph's title is its own heading/lead-in when it has one, otherwise
    the most recent titled paragraph above it. Paragraphs with no determinable
    title (e.g. a lone selected body paragraph whose heading was not sent) are
    omitted so the model's own label stays as the fallback.
    """

    titles: Dict[str, str] = {}
    current = ""
    first_seen = False
    for para in paragraphs:
        if not para.text.strip():
            continue
        own = _clause_title(para.text)
        # The document's own title line ("MASTER SERVICES AGREEMENT") reads like
        # a heading but is not a clause: an all-caps standalone heading opening
        # the payload never becomes a clause title for the paragraphs below it.
        stripped = para.text.strip()
        if not first_seen:
            first_seen = True
            if own and stripped == stripped.upper() and len(stripped.split()) >= 2:
                own = None
        if own:
            current = own
        if current:
            titles[para.paraindetifier] = current
            _remember_title(para.text, current)
        else:
            # A lone selected body paragraph carries no heading; reuse the title
            # this exact paragraph received in an earlier (whole-document) run.
            recalled = _recall_title(para.text)
            if recalled:
                titles[para.paraindetifier] = recalled
    return titles


def _lead_in(text: str) -> Optional[Tuple[str, str]]:
    """The verbatim title lead-in that opens a paragraph, plus its bare title.

    For 'Return of Advances.  In the event...' returns ('Return of Advances.  ',
    'Return of Advances'); for '7.3 Security Measures. Provider shall...' the
    lead includes the numbering. None when the paragraph has no inline title or
    is nothing but a heading.
    """

    stripped = text.lstrip()
    offset = len(text) - len(stripped)
    prefix = _TITLE_NUMBER_PREFIX_RE.match(stripped)
    base_start = prefix.end() if prefix else 0
    match = _INLINE_TITLE_RE.match(stripped[base_start:])
    if not match or not _looks_like_title(match.group(1).strip()):
        return None
    end = base_start + match.end()
    while end < len(stripped) and stripped[end] in " \t":
        end += 1
    lead = text[: offset + end]
    if lead.strip() == text.strip():
        return None
    return lead, match.group(1).strip()


def _preserve_lead_in(para_text: str, fix: str) -> str:
    """Re-attach the paragraph's clause-title lead-in when the fix dropped it.

    The model intermittently rewrites 'Return of Advances.  In the event...'
    as just 'In the event...', which would delete the clause heading when the
    fix is applied. If the paragraph opens with a title lead-in and the fix
    does not carry that title near its start, prepend the original lead-in.
    """

    found = _lead_in(para_text)
    if found is None:
        return fix
    lead, title = found
    title_norm = " ".join(title.split()).lower()
    fix_head = " ".join(fix.split())[: len(" ".join(lead.split())) + len(title_norm)].lower()
    if title_norm in fix_head:
        return fix
    return lead + fix.lstrip()


def _is_clause_start(text: str) -> bool:
    """A paragraph where a new clause begins — the only safe place to cut a batch."""

    stripped = text.strip()
    return _is_heading(stripped) or bool(_NUMBERED_START_RE.match(stripped))


def _context_tail(batch: _Batch) -> str:
    """Trailing paragraphs of a batch, carried as read-only context after a forced cut."""

    tail: List[str] = []
    size = 0
    for _, para in reversed(batch):
        text = para.text.strip()
        if size + len(text) > CONTEXT_TAIL_CHARS and tail:
            break
        tail.insert(0, text)
        size += len(text)
    return "\n\n".join(tail)


def _build_batches(paragraphs: Sequence[TextInformation]) -> List[Tuple[_Batch, str]]:
    """Split paragraphs into document-ordered (batch, context) pairs, cutting only at clause starts.

    Once a batch exceeds the char budget it closes at the next clause start
    (heading or numbered paragraph), so a clause's paragraphs always stay
    together. If a document has no detectable clause starts, the hard limit
    forces a cut and the next batch carries the previous tail as read-only
    context so nothing is judged without its surroundings.
    """

    indexed = [(idx, para) for idx, para in enumerate(paragraphs) if para.text.strip()]

    batches: List[Tuple[_Batch, str]] = []
    current: _Batch = []
    current_size = 0
    pending_context = ""

    for item in indexed:
        clean_cut = current_size >= BATCH_CHAR_BUDGET and _is_clause_start(item[1].text)
        forced_cut = current_size >= BATCH_HARD_LIMIT
        if current and (clean_cut or forced_cut):
            batches.append((current, pending_context))
            pending_context = _context_tail(current) if forced_cut else ""
            current = []
            current_size = 0
        current.append(item)
        current_size += len(item[1].text)

    if current:
        batches.append((current, pending_context))
    return batches


# Review calls run concurrently, so wall-clock is the SLOWEST call, not their
# sum. Grouping the document into at most this many balanced groups keeps every
# call in a single concurrent wave (MAX_CONCURRENT_CALLS) while making each call
# smaller than the old lopsided char-budget batches.
MAP_REVIEW_GROUPS = 6


def _clause_blocks(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> List[_Batch]:
    """Group paragraphs into whole clauses, using the map's verified clause identities.

    A new block starts wherever the map's clause name changes. These blocks are
    indivisible: a review call is assembled from whole clauses, so a clause is
    never split across two calls that each see only part of it.
    """

    blocks: List[_Batch] = []
    current: _Batch = []
    previous = None
    for position, para in enumerate(paragraphs):
        if not para.text.strip():
            continue
        title = titles.get(para.paraindetifier)
        if current and title is not None and title != previous:
            blocks.append(current)
            current = []
        current.append((position, para))
        if title is not None:
            previous = title
    if current:
        blocks.append(current)
    return blocks


def _pack_blocks(blocks: Sequence[_Batch], limit: int) -> List[_Batch]:
    """Pack whole clause blocks into groups, opening a new one when limit would be exceeded."""

    groups: List[_Batch] = []
    current: _Batch = []
    size = 0
    for block in blocks:
        block_size = sum(len(para.text) for _, para in block)
        if current and size + block_size > limit:
            groups.append(current)
            current = []
            size = 0
        current.extend(block)
        size += block_size
    if current:
        groups.append(current)
    return groups


def _build_map_batches(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> List[Tuple[_Batch, str]]:
    """Split the document into balanced review groups made of whole clauses.

    This replaces character-budget batching entirely when a document map is
    available. Two consequences, both deliberate:

    Correctness — a clause is never cut, so no call ever judges half a clause,
    and the read-only context tail the char batching needed disappears with it.

    Latency — the groups are balanced by binary-searching the smallest group
    size that still fits within MAP_REVIEW_GROUPS, which minimises the slowest
    call. The old batching produced 10638 / 10556 / 2761 character batches on a
    74-paragraph contract: the two large ones set the latency while the small
    one wasted a concurrency slot.
    """

    blocks = _clause_blocks(paragraphs, titles)
    if not blocks:
        return []

    total = sum(len(para.text) for block in blocks for _, para in block)
    biggest = max(sum(len(para.text) for _, para in block) for block in blocks)
    low = max(biggest, -(-total // MAP_REVIEW_GROUPS))
    high = max(low, total)

    best = _pack_blocks(blocks, high)
    while low <= high:
        middle = (low + high) // 2
        packed = _pack_blocks(blocks, middle)
        if len(packed) <= MAP_REVIEW_GROUPS:
            best = packed
            high = middle - 1
        else:
            low = middle + 1

    # No context tail: groups start on a clause boundary, so nothing is judged
    # without the rest of its own clause.
    return [(group, "") for group in best]


def _format_document(batch: _Batch) -> str:
    """Render a batch as plain paragraphs — identifiers stay out of the prompt."""

    return "\n\n".join(para.text.strip() for _, para in batch)


# Character families the model tends to normalize while copying; each matches all variants.
_APOSTROPHES = "'’‘"
_QUOTES = '"“”'
_DASHES = "-–—"


def _tolerant_pattern(needle: str) -> str:
    """Regex matching needle while tolerating whitespace runs and quote/dash variants."""

    tokens = []
    for token in needle.split():
        chars = []
        for char in token:
            if char in _APOSTROPHES:
                chars.append(f"[{_APOSTROPHES}]")
            elif char in _QUOTES:
                chars.append(f"[{_QUOTES}]")
            elif char in _DASHES:
                chars.append(f"[{re.escape(_DASHES)}]")
            else:
                chars.append(re.escape(char))
        tokens.append("".join(chars))
    return r"\s+".join(tokens)


def _ground(original_text: str, batch: _Batch) -> Optional[Tuple[int, TextInformation, str]]:
    """Find original_text inside a single paragraph and return (position, paragraph, exact source text).

    Exact match first; then a whitespace/quote-tolerant match that recovers the
    true verbatim substring, so the returned text always applies cleanly.
    """

    needle = original_text.strip()
    if not needle:
        return None

    for idx, para in batch:
        if needle in para.text:
            return idx, para, needle

    pattern = _tolerant_pattern(needle)
    for idx, para in batch:
        match = re.search(pattern, para.text)
        if match:
            return idx, para, match.group(0)
    return None


def _expand_to_paragraph(suggestion: Suggestion, para: TextInformation, exact_text: str) -> Tuple[str, str]:
    """Return (original, fix) covering the complete paragraph, splicing a partial fix if needed."""

    if exact_text.strip() == para.text.strip():
        return para.text, _preserve_lead_in(para.text, suggestion.suggested_fix)

    span = para.text.find(exact_text)
    prefix = para.text[:span]
    suffix = para.text[span + len(exact_text):]

    # If the model already returned a full-paragraph rewrite, splicing would duplicate text.
    fix = suggestion.suggested_fix
    already_full = (len(prefix.strip()) < 20 or prefix.strip()[:30] in fix) and (len(suffix.strip()) < 20 or suffix.strip()[-30:] in fix)
    full_fix = fix if already_full else prefix + fix + suffix
    return para.text, _preserve_lead_in(para.text, full_fix)


# A recovered spanning suggestion must replace exactly one paragraph; windows
# beyond this many paragraphs are treated as unrecoverable.
_RECOVERY_MAX_WINDOW = 6


def _recover_spanning(suggestion: Suggestion, batch: _Batch) -> Optional[Tuple[Suggestion, Tuple[int, TextInformation, str]]]:
    """Re-anchor a suggestion whose original_text merged several consecutive paragraphs.

    The model occasionally copies a clause together with its address block or
    signature lines into one original_text (rule 7 violation). Dropping it
    silently loses a real finding, so recover deterministically: find the run
    of consecutive paragraphs the text spans, identify the single paragraph
    whose content actually changed in the fix, extract that paragraph's
    replacement from the fix, and anchor the suggestion there. Anything
    ambiguous (zero or several changed paragraphs, text that is not a clean
    consecutive run) still returns None and gets dropped.
    """

    needle = suggestion.original_text.strip()
    if not needle:
        return None
    needle_re = re.compile(_tolerant_pattern(needle))

    for start in range(len(batch)):
        for end in range(start + 2, min(start + _RECOVERY_MAX_WINDOW, len(batch)) + 1):
            window = batch[start:end]
            joined = "\n".join(para.text.strip() for _, para in window)
            if abs(len(joined) - len(needle)) > 0.2 * len(needle) + 40:
                continue
            if not needle_re.fullmatch(joined):
                continue

            fix = suggestion.suggested_fix.strip()
            changed = [k for k, (_, para) in enumerate(window) if not re.search(_tolerant_pattern(para.text.strip()), fix)]
            if len(changed) != 1:
                return None

            parts = []
            for k, (_, para) in enumerate(window):
                parts.append(r"([\s\S]+?)" if k == changed[0] else _tolerant_pattern(para.text.strip()))
            match = re.fullmatch(r"\s*".join(parts), fix)
            if match is None or not match.group(1).strip():
                return None

            position, para = window[changed[0]]
            replacement = match.group(1).strip()
            updated = suggestion.model_copy(update={"original_text": para.text, "suggested_fix": replacement})
            return updated, (position, para, para.text)
    return None


# A bracketed token or a run of underscores — the shapes a blank takes when the
# model leaves a value for the parties to fill in ("[INSERT CAP AMOUNT]", "[__]").
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{0,80}\]|_{3,}")


def _inserted_placeholders(fix: str, original: str) -> List[str]:
    """Blanks the fix introduces that are not already in the source paragraph.

    Contracts legitimately contain brackets and underscore runs of their own —
    a signature line, or this test document's own malformed "[PayPal API". Those
    are copied through and must not be treated as defects, so a token only
    counts when the model added it.
    """

    return [token for token in _PLACEHOLDER_RE.findall(fix) if token not in original]


def _validate_batch(suggestions: List[Suggestion], batch: _Batch, seen_positions: Set[int], titles: Dict[str, str]) -> List[Tuple[int, Suggestion]]:
    """Ground each suggestion to one paragraph, expand it to the full paragraph, and attach its id and clause title."""

    valid: List[Tuple[int, Suggestion]] = []
    for suggestion in suggestions:
        grounded = _ground(suggestion.original_text, batch)
        if grounded is None:
            recovered = _recover_spanning(suggestion, batch)
            if recovered is None:
                logger.warning(
                    "Dropping suggestion for clause '%s' — original_text could not be grounded in a single source paragraph. original_text starts: %.120s",
                    suggestion.clause,
                    suggestion.original_text,
                )
                continue
            suggestion, grounded = recovered
            logger.info("Recovered a multi-paragraph suggestion for clause '%s' by re-anchoring it to paragraph %s.", suggestion.clause, grounded[1].paraindetifier)
        position, para, exact_text = grounded
        if position in seen_positions:
            continue
        original, full_fix = _expand_to_paragraph(suggestion, para, exact_text)

        # The frontend applies suggested_fix straight into the client's document,
        # so a fix carrying a blank would write "[INSERT CAP AMOUNT]" into a real
        # contract — worse than the defect it set out to repair. The prompt
        # forbids it; this is the guarantee that one never ships if it does not.
        placeholders = _inserted_placeholders(full_fix, original)
        if placeholders:
            logger.warning(
                "Dropping suggestion for clause '%s' — suggested_fix inserts %s, which would put a blank into the applied document.",
                suggestion.clause,
                ", ".join(repr(token) for token in placeholders[:3]),
            )
            continue

        seen_positions.add(position)
        # The document's own clause title keeps names identical across whole-doc
        # and selection runs; the model's label only fills in when the payload
        # carries no heading for this paragraph.
        clause = titles.get(para.paraindetifier, suggestion.clause)
        valid.append((position, suggestion.model_copy(update={"clause": clause, "original_text": original, "suggested_fix": full_fix, "para_identifier": para.paraindetifier})))

    valid.sort(key=lambda pair: pair[0])
    return valid


async def _review_batch(batch: _Batch, context: str, reviewer_context: str, semaphore: asyncio.Semaphore, session_id: str) -> GeneralReviewResponse:
    """Run the schema-enforced review call for one batch, with a single retry."""

    llm_model = get_bedrock_model()
    document = _format_document(batch)
    if context:
        document = f"Preceding portion of the document (read-only context — never raise suggestions for it):\n\n{context}\n\n--- TEXT TO REVIEW ---\n\n{document}"

    async with semaphore:
        # The retry resamples at a higher temperature: at temp 0 a malformed
        # response (e.g. the array serialized as a broken JSON string) would
        # just be reproduced verbatim.
        for attempt, temperature in ((1, 0.0), (2, 0.4)):
            try:
                return await llm_model.generate(
                    prompt=_GENERAL_REVIEW_USER,
                    context={"document": document, "reviewer_context": reviewer_context},
                    response_model=GeneralReviewResponse,
                    system_message=_GENERAL_REVIEW_SYSTEM,
                    session_id=session_id,
                    temperature=temperature,
                )
            except Exception:
                if attempt == 2:
                    raise
                logger.exception("Batch review failed on attempt %d; retrying once.", attempt)
                await asyncio.sleep(2)

    raise RuntimeError("unreachable")


def _consensus_risk(levels: List[str]) -> str:
    """Majority risk level across votes; median severity when no level has a majority."""

    top, count = Counter(levels).most_common(1)[0]
    if count * 2 > len(levels):
        return top
    ordered = sorted(levels, key=_RISK_ORDER.index)
    # Lower median on an even split, so a dead vote never inflates severity.
    return ordered[(len(ordered) - 1) // 2]


async def _review_batch_consensus(batch: _Batch, context: str, reviewer_context: str, semaphore: asyncio.Semaphore, session_id: str, titles: Dict[str, str]) -> List[Tuple[int, Suggestion]]:
    """Review one batch with parallel votes and keep only majority-backed findings."""

    outcomes = await asyncio.gather(*[_review_batch(batch, context, reviewer_context, semaphore, session_id) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)

    # A majority of votes must succeed; one lost vote never fails the review.
    votes = [outcome for outcome in outcomes if isinstance(outcome, GeneralReviewResponse)]
    if len(votes) < _MAJORITY:
        raise next(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    if len(votes) < CONSENSUS_VOTES:
        logger.warning("Batch consensus proceeding with %d of %d votes.", len(votes), CONSENSUS_VOTES)

    # One grounded finding per paragraph per vote, so vote counts stay honest.
    per_vote: List[Dict[int, Suggestion]] = []
    for vote in votes:
        grounded: Dict[int, Suggestion] = {}
        for position, suggestion in _validate_batch(vote.suggestions, batch, set(), titles):
            grounded.setdefault(position, suggestion)
        per_vote.append(grounded)

    counts = Counter(position for grounded in per_vote for position in grounded)
    picked: Dict[int, Suggestion] = {}
    for position, count in counts.items():
        if count < _MAJORITY:
            continue
        candidates = [grounded[position] for grounded in per_vote if position in grounded]
        # Prefer the most complete finding for this paragraph — the one whose
        # reason enumerates the most issues, approximated by reason length — so a
        # paragraph's secondary defect (a broken cross-reference, a typo) isn't
        # lost just because another vote only caught the primary issue.
        suggestion = max(candidates, key=lambda s: len(s.reason))
        # risk_level is voted separately: majority across the votes that raised
        # this paragraph, median severity when all votes differ.
        risk = _consensus_risk([candidate.risk_level for candidate in candidates])
        if suggestion.risk_level != risk:
            suggestion = suggestion.model_copy(update={"risk_level": risk})
        picked[position] = suggestion

    return sorted(picked.items())


async def general_review_service(request: GeneralReviewRequest, session_id: str, document_map: Optional[DocumentMap] = None) -> GeneralReviewResponse:
    """Review the full document or the selected clauses the frontend sent.

    When a document_map is supplied, its verified whole-document understanding
    (parties, roles, defined terms, known issues) is injected as shared context
    so every batch reviews against one consistent reading; the caches are salted
    by the map so grounded and ungrounded runs never mix.
    """

    salt = _map_salt(document_map)
    fingerprint = _request_fingerprint(request, salt)
    cached = _cache_get(fingerprint)
    if cached is not None:
        logger.info("General review: cache hit — returning the stored review for this document.")
        return cached

    remembered, fresh = _recall_outcomes(request, salt)
    fresh_paras = [para for _, para in fresh]
    if remembered:
        logger.info("General review: replaying %d remembered paragraph outcome(s); %d paragraph(s) go to a fresh review.", len(remembered), len(fresh_paras))

    reviewer_context = _build_reviewer_context(request) + _render_document_map_context(document_map)
    # With a document map, its verified clause identities are the single source
    # of truth for clause names — the regex heuristic is not used at all, since
    # it is precisely what mislabels clauses it cannot parse. A paragraph the map
    # has no operative clause for (e.g. a recital or the preamble) keeps the
    # model's own label, which is reliable here because the model reviews with
    # the shared map as context. Without a map, the regex remains the fallback.
    titles = _map_clause_titles(document_map, request.textinformation) if document_map is not None else _build_clause_titles(request.textinformation)

    # The map's clause identities also decide where review calls are cut, so a
    # call never receives half a clause. This needs the map to have actually
    # named these paragraphs: with no names there are no clause boundaries, and
    # clause grouping would silently collapse the whole document into a single
    # call. In that case the char budget is the safer split.
    grouped_by_clause = document_map is not None and bool(titles)
    batches = _build_map_batches(fresh_paras, titles) if grouped_by_clause else _build_batches(fresh_paras)
    if not batches and not remembered:
        return GeneralReviewResponse(suggestions=[])

    logger.info(
        "General review: %d paragraph(s) split into %d %s group(s), %d vote(s) each%s.",
        len(request.textinformation),
        len(batches),
        "clause-aligned" if grouped_by_clause else "char-budget",
        CONSENSUS_VOTES,
        ", with reviewer context" if reviewer_context else "",
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    results = await asyncio.gather(*[_review_batch_consensus(batch, context, reviewer_context, semaphore, session_id, titles) for batch, context in batches])

    # Batch positions index the fresh list; translate back to document positions.
    seen_positions: Set[int] = set()
    positioned: List[Tuple[int, Suggestion]] = []
    for batch_result in results:
        for fresh_position, suggestion in batch_result:
            original_position = fresh[fresh_position][0]
            if original_position in seen_positions:
                continue
            seen_positions.add(original_position)
            positioned.append((original_position, suggestion))

    # Every freshly reviewed paragraph is remembered: its suggestion, or clean.
    flagged_by_fresh_position = {fresh_position: suggestion for batch_result in results for fresh_position, suggestion in batch_result}
    _remember_fresh_outcomes(fresh, flagged_by_fresh_position, request, salt)

    for original_position, outcome in remembered.items():
        if outcome is not None and original_position not in seen_positions:
            seen_positions.add(original_position)
            positioned.append((original_position, outcome))

    positioned.sort(key=lambda pair: pair[0])
    response = GeneralReviewResponse(suggestions=[suggestion for _, suggestion in positioned])
    _cache_put(fingerprint, response)
    return response


async def general_review_streaming_service(request: GeneralReviewRequest, session_id: str) -> AsyncIterator[str]:
    """Stream the review as SSE text fragments that concatenate into one valid JSON response.

    Batches run in parallel; each batch's validated suggestions are emitted in
    document order as soon as that batch completes.
    """

    def frame(fragment: str) -> str:
        return f"data: {json.dumps(fragment)}\n\n"

    tasks: List[asyncio.Task] = []
    try:
        fingerprint = _request_fingerprint(request)
        cached = _cache_get(fingerprint)
        if cached is not None:
            logger.info("General review stream: cache hit — streaming the stored review for this document.")
            yield frame('{"suggestions": [')
            for index, suggestion in enumerate(cached.suggestions):
                separator = "" if index == 0 else ","
                yield frame(separator + suggestion.model_dump_json())
            yield frame("]}")
            yield "data: [DONE]\n\n"
            return

        remembered, fresh = _recall_outcomes(request)
        fresh_paras = [para for _, para in fresh]
        batches = _build_batches(fresh_paras)
        reviewer_context = _build_reviewer_context(request)
        titles = _build_clause_titles(request.textinformation)
        if remembered:
            logger.info("General review stream: replaying %d remembered paragraph outcome(s); %d paragraph(s) go to a fresh review.", len(remembered), len(fresh_paras))
        logger.info("General review stream: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else "")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [asyncio.create_task(_review_batch_consensus(batch, context, reviewer_context, semaphore, session_id, titles)) for batch, context in batches]

        # Remembered suggestions interleave with fresh batch results in document
        # order: each batch's results are merged with the replayed items that
        # fall before the next batch begins.
        replayed = sorted((position, outcome) for position, outcome in remembered.items() if outcome is not None)
        batch_starts = [fresh[batch[0][0]][0] for batch, _ in batches]

        yield frame('{"suggestions": [')

        emitted: List[Suggestion] = []
        seen_positions: Set[int] = set()
        replay_index = 0
        flagged_by_fresh_position: Dict[int, Suggestion] = {}

        for task_index, task in enumerate(tasks):
            translated = []
            for fresh_position, suggestion in await task:
                flagged_by_fresh_position[fresh_position] = suggestion
                translated.append((fresh[fresh_position][0], suggestion))
            boundary = batch_starts[task_index + 1] if task_index + 1 < len(batch_starts) else float("inf")
            while replay_index < len(replayed) and replayed[replay_index][0] < boundary:
                translated.append(replayed[replay_index])
                replay_index += 1
            for position, suggestion in sorted(translated, key=lambda pair: pair[0]):
                if position in seen_positions:
                    continue
                seen_positions.add(position)
                separator = "" if not emitted else ","
                yield frame(separator + suggestion.model_dump_json())
                emitted.append(suggestion)

        for position, suggestion in replayed[replay_index:]:
            if position in seen_positions:
                continue
            seen_positions.add(position)
            separator = "" if not emitted else ","
            yield frame(separator + suggestion.model_dump_json())
            emitted.append(suggestion)

        yield frame("]}")

        _remember_fresh_outcomes(fresh, flagged_by_fresh_position, request)
        _cache_put(fingerprint, GeneralReviewResponse(suggestions=emitted))

    except Exception as exc:
        logger.exception("General review stream failed: %s", exc)
        for task in tasks:
            task.cancel()
        yield f"data: {json.dumps({'error': 'General review failed. Please try again.'})}\n\n"

    yield "data: [DONE]\n\n"
