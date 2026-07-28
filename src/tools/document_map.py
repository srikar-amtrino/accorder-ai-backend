import asyncio
import hashlib
import re
from collections import OrderedDict
from math import ceil
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from src.config.logging import get_logger
from src.schemas.document_map import Ambiguity, Clause, ClauseBoundaries, ClauseStart, DefinedTerm, DocumentMap, Party
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)

# A document at or under this size is mapped in a single call. Above it the
# document is cut into sections that are extracted concurrently, so wall-clock
# tracks the slowest section instead of the sum — the main latency lever on long
# contracts.
#
# A cut may fall ONLY at a paragraph where a clause begins (see
# _clause_start_indexes). Cutting on size alone would hand half a clause to one
# call and half to another, neither aware of the other, which loses or
# mis-identifies exactly the clauses that matter most.
_SECTION_CHAR_BUDGET = 9000

# Sections run concurrently, so the section count is capped at the concurrency
# limit: past that point extra sections queue instead of overlapping and only
# add latency. Long documents therefore get FEWER, larger sections rather than a
# growing queue of small ones.
_MAX_CONCURRENT_SECTIONS = 6

# Identical documents return the identical map without re-calling the model, so
# a review that reuses the map (or a repeat extraction) is instant. In-process
# performance cache only — this is not the persistent store (that decision is
# deferred); it is lost on restart, exactly like the existing session caches.
_MAP_CACHE_MAX = 64
_map_cache: "OrderedDict[str, DocumentMap]" = OrderedDict()


def _get_bedrock_model() -> BaseLLMModel:
    """Return the shared Bedrock model.

    The document-understanding pass is a pre-pass that depends only on the LLM,
    not on the vector-store/ingestion stack the DI container eagerly builds at
    import. Prefer the container's already-constructed instance when the app is
    running; fall back to a standalone BedrockModel (e.g. in the demo harness),
    so this layer never drags in faiss or the embedding model just to read a
    document.
    """

    try:
        from src.core.container import get_bedrock_model as _from_container

        return _from_container()
    except Exception:
        from src.services.llm.bedrock_model import BedrockModel

        return BedrockModel()

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "document_map"
_DOCUMENT_MAP_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_DOCUMENT_MAP_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

_BOUNDARY_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "clause_boundaries"
_CLAUSE_BOUNDARY_SYSTEM = (_BOUNDARY_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_CLAUSE_BOUNDARY_USER = (_BOUNDARY_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# The boundary pass answers only for the few places a cut would fall, with an
# index and a few words each — no names, summaries, or categories. Output size
# is what costs wall-clock, so this is a ceiling far above what it should ever
# need, not a target.
_CLAUSE_BOUNDARY_MAX_TOKENS = 1000

# How far from the index the model reported to look for the paragraph its
# opening_words actually match, when the two disagree (an off-by-a-few count).
_BOUNDARY_SEARCH_WINDOW = 6

# A whole-document map (parties, terms, every clause with a verbatim anchor)
# runs larger than a single review batch, so it gets more output headroom than
# the 10k default to avoid a truncated, invalid response on long contracts.
_DOCUMENT_MAP_MAX_TOKENS = 16000

# Character families the model tends to normalize while copying; each matches
# all variants, so a clause anchor still grounds when the model swaps a curly
# quote or an en dash for its plain form. Mirrors general_review's grounding.
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


def _first_paragraph_of(anchor_text: str) -> str:
    """The part of an anchor that lies inside its own first paragraph.

    The model reliably starts an anchor at the right place but often keeps
    copying past the paragraph break — a heading, the blank line after it, and
    then the body all land in one anchor ("Non-Compete.\\n\\nDuring the term
    of..."), and an address block can swallow eight paragraphs. Cutting at the
    first line break restores the single-paragraph anchor every consumer
    expects. Doing it here makes it a property of the data rather than
    something the prompt has to get right every time.
    """

    return re.split(r"[\r\n]+", anchor_text.strip(), maxsplit=1)[0].strip()


def _anchor_paragraph(anchor_text: str, paragraphs: Sequence[str], cursor: int) -> Optional[int]:
    """The single paragraph an anchor resolves to, or None when it resolves to none.

    Grounding is deliberately checked against ONE PARAGRAPH AT A TIME rather
    than the joined document. An anchor that runs across a paragraph break is a
    substring of the joined text and would look grounded, yet every consumer
    matches anchors per paragraph — so it silently resolves to nothing and its
    clause loses its identity. Checking per paragraph turns that into a visible
    failure.

    The search starts at the previous clause's paragraph (clauses arrive in
    document order, and one paragraph can hold several combined clauses) and
    only falls back to a full scan if that finds nothing.
    """

    needle = anchor_text.strip()
    if not needle:
        return None

    pattern = _tolerant_pattern(needle)
    for scan_from in (cursor, 0):
        for index in range(scan_from, len(paragraphs)):
            if needle in paragraphs[index]:
                return index
        for index in range(scan_from, len(paragraphs)):
            if re.search(pattern, paragraphs[index]):
                return index
        if scan_from == 0:
            break
    return None


def _ground_clauses(clauses: Sequence[Clause], paragraphs: Sequence[str]) -> List[Clause]:
    """Stamp each clause with whether its anchor resolves to a single source paragraph.

    A clause whose anchor cannot be resolved did not come from the document as
    written, or spans a paragraph break — its identity may still be right, but
    it is no longer trusted as grounded, so it is downgraded to 'flagged' for
    the reviewer's attention rather than presented as a clean, certain reading.
    """

    grounded_clauses: List[Clause] = []
    ungrounded = 0
    trimmed = 0
    cursor = 0
    for clause in clauses:
        anchor = _first_paragraph_of(clause.anchor_text)
        index = _anchor_paragraph(anchor, paragraphs, cursor)
        grounded = index is not None
        if index is not None:
            # Not index + 1: several combined clauses can share one paragraph.
            cursor = index
        updates: Dict[str, Any] = {"grounded": grounded}
        if anchor != clause.anchor_text.strip():
            # Store the trimmed anchor, so consumers that match per paragraph
            # get one that can actually resolve.
            updates["anchor_text"] = anchor
            trimmed += 1
        if not grounded and clause.confidence != "flagged":
            updates["confidence"] = "flagged"
            ungrounded += 1
        grounded_clauses.append(clause.model_copy(update=updates))
    if trimmed:
        logger.info("Document map: trimmed %d clause anchor(s) that ran past their own paragraph.", trimmed)
    if ungrounded:
        logger.warning("Document map: %d clause anchor(s) did not resolve to a single source paragraph and were flagged.", ungrounded)
    return grounded_clauses


def _document_from_paragraphs(paragraphs: Sequence[str]) -> str:
    """Join paragraphs into the single document string the model reads and we ground against."""

    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


# The map built for a session, kept so a later request can reuse it without the
# document. This is what makes SELECTION mode work: the frontend sends only the
# selected paragraphs, which can never yield a whole-document map, so the map
# must already have been built — at document open — and found by session.
#
# In-process, exactly like the existing session caches: it does not survive a
# restart and is not shared between instances. A miss is never fatal — the
# caller falls back to building a map from whatever it was given.
_SESSION_MAP_MAX = 256
_session_maps: "OrderedDict[str, Tuple[DocumentMap, Set[str]]]" = OrderedDict()

# A stored map is only handed back when at least this share of the requesting
# paragraphs actually come from the document it was built on. Not 100%, because
# the user edits the contract in Word between opening it and asking for a
# review, and a handful of changed paragraphs must not throw the map away.
_SESSION_MAP_MIN_MATCH = 0.5


def _paragraph_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _paragraph_keys(paragraphs: Sequence[str]) -> Set[str]:
    return {_paragraph_key(para) for para in paragraphs if para.strip()}


def remember_session_map(session_id: str, document_map: DocumentMap, paragraphs: Sequence[str]) -> None:
    """Store this session's map together with the document it was built from."""

    if not session_id:
        return
    _session_maps[session_id] = (document_map, _paragraph_keys(paragraphs))
    _session_maps.move_to_end(session_id)
    while len(_session_maps) > _SESSION_MAP_MAX:
        _session_maps.popitem(last=False)


def recall_session_map(session_id: str, paragraphs: Sequence[str]) -> Optional[DocumentMap]:
    """This session's map, but only if these paragraphs really came from that document.

    A session id alone is not proof. The same session can be reused after the
    user switches documents, and handing back the previous document's map would
    tell the reviewer, as authoritative fact, that the contract in front of it
    has parties and defined terms belonging to a different agreement. It would
    still return a confident-looking review — the worst possible failure.

    So membership is checked rather than assumed, which also works for a
    selection: a handful of paragraphs from the mapped document still match.
    """

    entry = _session_maps.get(session_id)
    if entry is None:
        return None

    document_map, known = entry
    wanted = _paragraph_keys(paragraphs)
    if not wanted:
        return None

    matched = len(wanted & known)
    if matched < _SESSION_MAP_MIN_MATCH * len(wanted):
        logger.warning(
            "Session has a stored document map, but only %d of %d paragraph(s) in this request come from that document. "
            "Ignoring it — reviewing one document against another document's map would be worse than having no map.",
            matched,
            len(wanted),
        )
        return None

    _session_maps.move_to_end(session_id)
    return document_map


def _cache_key(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _cache_put(key: str, document_map: DocumentMap) -> None:
    _map_cache[key] = document_map
    _map_cache.move_to_end(key)
    while len(_map_cache) > _MAP_CACHE_MAX:
        _map_cache.popitem(last=False)


def _numbered_document(paragraphs: Sequence[str]) -> str:
    """Render the paragraphs with a bracketed index each, for the boundary pass."""

    return "\n\n".join(f"[{index}] {para.strip()}" for index, para in enumerate(paragraphs))


def _head_words(text: str, count: int) -> str:
    """The first count words of text, lowercased and stripped of punctuation.

    Used to check that a reported boundary index really points at the paragraph
    whose opening words the model copied, tolerating the punctuation and casing
    differences that creep into a copied excerpt.
    """

    tokens = re.sub(r"[^\w\s]", " ", text).split()
    return " ".join(tokens[:count]).lower()


def _nearby(index: int, total: int) -> Iterator[int]:
    """The reported index first, then the indexes closest to it, within the window."""

    if 0 <= index < total:
        yield index
    for offset in range(1, _BOUNDARY_SEARCH_WINDOW + 1):
        for candidate in (index - offset, index + offset):
            if 0 <= candidate < total:
                yield candidate


def _match_index(start: ClauseStart, paragraphs: Sequence[str]) -> Optional[int]:
    """The paragraph this boundary actually refers to, or None when it cannot be verified.

    The model's opening_words are the ground truth, not its index: a miscounted
    index is corrected to the nearby paragraph those words really open. A
    boundary whose words match no paragraph nearby is unverifiable, and an
    unverified boundary is worse than no boundary at all — it would authorize a
    cut in the middle of a clause — so it is discarded.
    """

    total = len(paragraphs)
    words = _head_words(start.opening_words, 8).split()
    if len(words) < 2:
        # Too short to verify against the source; accept it only as an in-range index.
        return start.paragraph_index if 0 <= start.paragraph_index < total else None

    target = " ".join(words)
    for candidate in _nearby(start.paragraph_index, total):
        if _head_words(paragraphs[candidate], len(words)) == target:
            return candidate
    return None


def _verified_starts(clause_starts: Sequence[ClauseStart], paragraphs: Sequence[str]) -> Tuple[Set[int], int]:
    """Verify every reported boundary against the source; return (indexes, discarded count)."""

    starts: Set[int] = {0}
    discarded = 0
    for start in clause_starts:
        index = _match_index(start, paragraphs)
        if index is None:
            discarded += 1
            continue
        starts.add(index)
    return starts, discarded


# Fallback clause-start detection, used only when the boundary pass fails or
# returns nothing usable. Numbering and short standalone headings are the two
# signals that can be read without the model; they are exactly what a messy
# document lacks, which is why this is the fallback and not the primary path.
_NUMBERED_START_RE = re.compile(r"^\s*(?:(?:section|article|clause)\s+)?(?:\d+(?:\.\d+)*|[ivxlc]+|[a-z])[.)]\s+\S", re.IGNORECASE)
_HEADING_MAX_CHARS = 80
_HEADING_MAX_WORDS = 8


def _heuristic_clause_starts(paragraphs: Sequence[str]) -> Set[int]:
    """Clause starts readable from numbering and standalone headings alone."""

    starts: Set[int] = {0}
    for index, para in enumerate(paragraphs):
        stripped = para.strip()
        is_heading = 0 < len(stripped) <= _HEADING_MAX_CHARS and len(stripped.split()) <= _HEADING_MAX_WORDS
        if is_heading or _NUMBERED_START_RE.match(stripped):
            starts.add(index)
    return starts


# Sections are read concurrently, so the wait is the LARGEST section, not their
# sum — which means the goal is to use every concurrency slot, not to fill each
# section to the budget. A 24k document divided by the 9k budget yields only 3
# sections and leaves half the pool idle; spread over 6 it halves the wait for
# the same total work.
#
# The floor stops that going too far: below this, a section is too thin to read
# a clause well and the fixed prompt overhead starts to dominate each call.
_MIN_SECTION_CHARS = 3500


def _wanted_sections(total_chars: int) -> int:
    """How many concurrent sections this document should be divided into."""

    if total_chars <= _SECTION_CHAR_BUDGET:
        return 1
    return max(2, min(_MAX_CONCURRENT_SECTIONS, total_chars // _MIN_SECTION_CHARS))


# Candidate cut points are requested at this multiple of the sections actually
# wanted. The model answers with the nearest real clause start to each target,
# which can land far from it — with only the bare minimum of candidates the
# section sizes are then whatever the clause structure happens to give (a 2k
# section beside a 5.7k one, and the 5.7k one sets the wait). Extra candidates
# cost a few output tokens and give the packer room to even the sections out.
_CUT_CANDIDATE_FACTOR = 2


def _cut_targets(paragraphs: Sequence[str], wanted: int) -> List[int]:
    """The paragraph indexes where evenly-spaced section boundaries would ideally fall.

    These are only targets. The model answers with the nearest paragraph that
    actually begins a clause, and those answers become the candidate cuts the
    packer chooses from.
    """

    total = sum(len(para) for para in paragraphs)
    divisions = wanted * _CUT_CANDIDATE_FACTOR
    targets: List[int] = []
    for part in range(1, divisions):
        goal = part * total / divisions
        running = 0
        for index, para in enumerate(paragraphs):
            running += len(para)
            if running >= goal:
                if not targets or index > targets[-1]:
                    targets.append(index)
                break
    return targets


async def _clause_start_indexes(paragraphs: Sequence[str], session_id: str) -> Set[int]:
    """The paragraph indexes at which the document may be divided into sections.

    The model sees the ENTIRE document in one call, so it judges a boundary the
    way a lawyer does — by what a paragraph does, not by whether it happens to
    be numbered — and unnumbered, unheaded or run-together clauses are handled
    rather than missed.

    It is asked only about the handful of places a cut would actually fall, not
    about every clause in the document. Reading the whole document is cheap;
    WRITING the answer is what costs wall-clock, so the answer is kept to a few
    entries. Asking for the full clause list instead measured ~25s on a 74
    paragraph contract versus a few seconds for this, for identical cuts.

    There is no retry. On failure the heuristic fallback answers immediately,
    which costs less wall-clock than a second call and still yields safe cut
    points on any document that carries numbering or headings.
    """

    targets = _cut_targets(paragraphs, _wanted_sections(sum(len(para) for para in paragraphs)))
    if not targets:
        return {0}

    llm_model = _get_bedrock_model()
    try:
        boundaries: ClauseBoundaries = await llm_model.generate(  # type: ignore[assignment]
            prompt=_CLAUSE_BOUNDARY_USER,
            context={"document": _numbered_document(paragraphs), "targets": ", ".join(str(target) for target in targets)},
            response_model=ClauseBoundaries,
            system_message=_CLAUSE_BOUNDARY_SYSTEM,
            session_id=session_id,
            temperature=0.0,
            max_tokens=_CLAUSE_BOUNDARY_MAX_TOKENS,
        )
    except Exception:
        logger.exception("Clause boundary pass failed; falling back to heuristic clause starts.")
        return _heuristic_clause_starts(paragraphs)

    starts, discarded = _verified_starts(boundaries.clause_starts, paragraphs)
    if discarded:
        logger.warning("Clause boundary pass: discarded %d boundary/boundaries that could not be verified against the source.", discarded)
    if len(starts) <= 1:
        logger.warning("Clause boundary pass returned no usable boundaries; falling back to heuristic clause starts.")
        return _heuristic_clause_starts(paragraphs)

    logger.info("Clause boundary pass: %d verified clause start(s) across %d paragraph(s).", len(starts), len(paragraphs))
    return starts


def _clause_blocks(paragraphs: Sequence[str], clause_starts: Set[int]) -> List[List[str]]:
    """Group the paragraphs into whole clauses — the indivisible units of extraction.

    A block runs from one clause start up to the next. Sections are assembled
    from these blocks and never from paragraphs, which is what makes splitting a
    clause structurally impossible rather than merely unlikely.
    """

    blocks: List[List[str]] = []
    current: List[str] = []
    for index, para in enumerate(paragraphs):
        if current and index in clause_starts:
            blocks.append(current)
            current = []
        current.append(para)
    if current:
        blocks.append(current)
    return blocks


def _pack(blocks: Sequence[List[str]], limit: int) -> List[List[str]]:
    """Pack whole clause blocks into sections, opening a new one when limit would be exceeded.

    A block larger than the limit still gets a section of its own — an oversized
    clause is never broken up to satisfy a size target.
    """

    sections: List[List[str]] = []
    current: List[str] = []
    size = 0
    for block in blocks:
        block_size = sum(len(para) for para in block)
        if current and size + block_size > limit:
            sections.append(current)
            current = []
            size = 0
        current.extend(block)
        size += block_size
    if current:
        sections.append(current)
    return sections


def _split_sections(paragraphs: Sequence[str], clause_starts: Set[int]) -> List[List[str]]:
    """Cut the paragraphs into balanced, concurrently-extractable sections at clause starts.

    Two properties matter here, in this order:

    Correctness — sections are assembled from whole clause blocks, so no clause
    is ever split across two calls that each see half of it. A clause larger
    than the target keeps its own section intact. The document is never cut on
    size alone.

    Latency — every section runs concurrently, so wall-clock is the LARGEST
    section, not the sum. Rather than closing sections greedily (which leaves
    one oversized section on the critical path), search for the smallest
    section size that still fits the document into the wanted number of
    sections. That directly minimizes the slowest call, which is the only
    number that shows up as latency.
    """

    total = sum(len(para) for para in paragraphs)
    wanted = _wanted_sections(total)
    if wanted <= 1:
        return [list(paragraphs)]

    blocks = _clause_blocks(paragraphs, clause_starts)
    if len(blocks) <= 1:
        return [list(paragraphs)]

    # Binary-search the smallest per-section size that still fits in `wanted`
    # sections. The floor is whichever is larger: the biggest single clause
    # (indivisible) or an even split of the document.
    low = max(max(sum(len(para) for para in block) for block in blocks), ceil(total / wanted))
    high = max(low, total)
    best = _pack(blocks, high)
    while low <= high:
        mid = (low + high) // 2
        packed = _pack(blocks, mid)
        if len(packed) <= wanted:
            best = packed
            high = mid - 1
        else:
            low = mid + 1
    return best


# Prepended to a section so the model treats it as part of a larger contract and
# does not invent — or falsely flag — parties and terms established elsewhere.
_SECTION_NOTE = (
    "NOTE: The text below is ONE SECTION of a larger contract, not the whole document. Other sections exist "
    "that you cannot see. Extract only the parties, defined terms, clauses, and ambiguities that actually "
    "appear in the text below.\n\n"
    "Because you cannot see the rest of the document, you must NEVER state that something is absent from the "
    "Agreement — you have no way to know that:\n"
    "- Never report that the Agreement lacks a clause, term, date, duration, party, or any other provision. "
    "Another section very likely contains it. Describe only what this text does contain.\n"
    "- Never flag a capitalized term as undefined merely because its definition is not in this section.\n"
    "- Record an ambiguity only when the DEFECT ITSELF is visible in the text below: a broken cross-reference, "
    "an unfilled placeholder, a garbled sentence, or a contradiction between two passages that are BOTH present "
    "here. An absence you infer from not being shown something is not a defect you have observed.\n\n"
    "Never refer to this excerpt in ANY text you write — not in source_location, not in a meaning, summary, or "
    "description. Phrases such as 'this section', 'the first clause of this section', 'not defined in this "
    "section' or 'the section above' are meaningless in the final result, which is assembled from many excerpts "
    "and read by someone who never sees the split. Write every field as if you had read the whole document: "
    "locate things by the document's own numbering or headings ('Section 4', 'Term clause', 'Preamble'), and "
    "use null for source_location when the document offers no location of its own.\n\n"
)


async def _extract_map(document: str, session_id: str, section_mode: bool) -> DocumentMap:
    """One schema-enforced Bedrock pass over a document (or one section of it).

    A retry resamples at a higher temperature because at temperature 0 a
    malformed response would just be reproduced verbatim.
    """

    llm_model = _get_bedrock_model()
    text = (_SECTION_NOTE + document) if section_mode else document

    for attempt, temperature in ((1, 0.0), (2, 0.4)):
        try:
            return await llm_model.generate(  # type: ignore[return-value]
                prompt=_DOCUMENT_MAP_USER,
                context={"document": text},
                response_model=DocumentMap,
                system_message=_DOCUMENT_MAP_SYSTEM,
                session_id=session_id,
                temperature=temperature,
                max_tokens=_DOCUMENT_MAP_MAX_TOKENS,
            )
        except Exception:
            if attempt == 2:
                raise
            logger.exception("Document map section failed on attempt %d; retrying once.", attempt)
            await asyncio.sleep(2)

    raise RuntimeError("unreachable")


_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "lp", "llp", "plc", "gmbh", "pvt", "private"}


def _entity_key(name: str) -> str:
    """Normalize an entity name for matching: lowercased, punctuation and legal suffixes stripped.

    Merges "XYZInc" with "XYZInc Inc." and "Amtrino Technologies" with "Amtrino
    Technologies LLC" so the same party found in different sections is one entry.
    """

    tokens = [token for token in re.sub(r"[.,]", " ", name).lower().split() if token not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def _better_party(a: Party, b: Party) -> Party:
    """Pick the fuller of two records for the same party: one with a role wins,
    then the longer actual name (a real name beats a bare label)."""

    a_score = (bool(a.role), len(a.name_as_written))
    b_score = (bool(b.role), len(b.name_as_written))
    return a if a_score >= b_score else b


# The subject of an ambiguity is the first phrase its description quotes —
# these descriptions open with the term at issue ("'Epit UserWeb' is listed...").
_QUOTED_RE = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,80})[\"'“”‘’]")


def _ambiguity_subject(description: str) -> str:
    """The term an ambiguity is about, normalized for comparison."""

    match = _QUOTED_RE.search(description)
    return " ".join(match.group(1).split()).lower() if match else ""


def _reconcile_ambiguities(ambiguities: Sequence[Ambiguity], defined_terms: Sequence[DefinedTerm]) -> List[Ambiguity]:
    """Drop 'undefined term' findings for terms that another section actually defined.

    A section-scoped call cannot see the rest of the document, so it may report a
    term as never defined when a later section defines it outright. Once the
    sections are merged that claim is simply false — and unlike most such claims
    it is mechanically checkable, so it is checked rather than shipped.

    Deliberately conservative: a definition only counts if it carries a real
    meaning AND was not itself flagged. A term the document only half-defines
    stays flagged, because there the finding is true.
    """

    defined = {" ".join(term.term.split()).lower() for term in defined_terms if term.meaning and term.confidence != "flagged"}
    if not defined:
        return list(ambiguities)

    kept: List[Ambiguity] = []
    dropped = 0
    for item in ambiguities:
        if "undefined" in item.kind.lower() and _ambiguity_subject(item.description) in defined:
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        logger.info("Document map: dropped %d undefined-term ambiguity/ies for term(s) defined in another section.", dropped)
    return kept


def _merge_maps(partials: Sequence[DocumentMap]) -> DocumentMap:
    """Merge per-section maps into one: dedup parties and terms, concatenate clauses in order.

    Parties and defined terms recur across sections, so they are de-duplicated
    (a party by its defined_as label, a term by its name); a later entry that
    adds a role/meaning upgrades an earlier bare one. Clauses and ambiguities are
    section-local and kept in document order.
    """

    contract_type = next((p.contract_type for p in partials if p.contract_type), None)

    parties: List[Party] = []
    key_to_idx: Dict[str, int] = {}
    for partial in partials:
        for party in partial.parties:
            name_key = _entity_key(party.name_as_written)
            label_key = _entity_key(party.defined_as)
            idx = key_to_idx.get(name_key) if name_key else None
            if idx is None and label_key:
                idx = key_to_idx.get(label_key)
            if idx is None:
                idx = len(parties)
                parties.append(party)
            else:
                parties[idx] = _better_party(parties[idx], party)
            # Register both keys so the same entity merges whether a later section
            # names it by its actual name or by its defined label.
            if name_key:
                key_to_idx.setdefault(name_key, idx)
            if label_key:
                key_to_idx.setdefault(label_key, idx)

    terms: List[DefinedTerm] = []
    term_at: Dict[str, int] = {}
    for partial in partials:
        for term in partial.defined_terms:
            key = " ".join(term.term.split()).lower()
            if not key:
                continue
            if key not in term_at:
                term_at[key] = len(terms)
                terms.append(term)
            elif term.meaning and not terms[term_at[key]].meaning:
                terms[term_at[key]] = term

    clauses = [clause for partial in partials for clause in partial.clauses]
    ambiguities = _reconcile_ambiguities([item for partial in partials for item in partial.ambiguities], terms)

    return DocumentMap(contract_type=contract_type, parties=parties, defined_terms=terms, clauses=clauses, ambiguities=ambiguities)


async def build_document_map_from_paragraphs(paragraphs: Sequence[str], session_id: str) -> DocumentMap:
    """Build the grounded DocumentMap from the paragraph list the agents work with.

    Cached by document hash (identical input returns instantly).

    A short document is read in one call. A long one first goes through the
    clause-boundary pass, which reads the WHOLE document at once and reports
    where every clause begins; the document is then cut at those boundaries only
    and the sections are extracted concurrently and merged. So a clause is never
    handed to two calls in halves, while latency still tracks the slowest section
    rather than the whole document. Every clause anchor is finally grounded
    against the full source text.
    """

    document = _document_from_paragraphs(paragraphs)
    key = _cache_key(document)
    cached = _map_cache.get(key)
    if cached is not None:
        _map_cache.move_to_end(key)
        remember_session_map(session_id, cached, paragraphs)
        logger.info("Document map: cache hit — returning the stored map for this document.")
        return cached

    clean = [para.strip() for para in paragraphs if para.strip()]
    total = sum(len(para) for para in clean)

    # Short enough to read in one call: no boundary pass, no sectioning, nothing
    # to split — so nothing to get wrong, and one call is also the fastest path.
    if total <= _SECTION_CHAR_BUDGET:
        sections: List[List[str]] = [clean]
    else:
        sections = _split_sections(clean, await _clause_start_indexes(clean, session_id))

    if len(sections) <= 1:
        document_map = await _extract_map(document, session_id, section_mode=False)
    else:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SECTIONS)

        async def _run(section_paras: List[str]) -> DocumentMap:
            async with semaphore:
                return await _extract_map(_document_from_paragraphs(section_paras), session_id, section_mode=True)

        partials = await asyncio.gather(*[_run(section) for section in sections])
        document_map = _merge_maps(list(partials))

    grounded = _ground_clauses(document_map.clauses, clean)
    document_map = document_map.model_copy(update={"clauses": grounded})
    _cache_put(key, document_map)
    remember_session_map(session_id, document_map, paragraphs)

    logger.info(
        "Document map built: %d section(s), type=%s, %d part(y/ies), %d defined term(s), %d clause(s), %d ambiguit(y/ies).",
        len(sections),
        document_map.contract_type,
        len(document_map.parties),
        len(document_map.defined_terms),
        len(document_map.clauses),
        len(document_map.ambiguities),
    )
    return document_map


async def build_document_map(document: str, session_id: str) -> DocumentMap:
    """Build the grounded DocumentMap for a document passed as a single string."""

    paragraphs = [para for para in document.split("\n\n") if para.strip()]
    return await build_document_map_from_paragraphs(paragraphs, session_id)
