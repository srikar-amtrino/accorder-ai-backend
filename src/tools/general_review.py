import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_clause_search
from src.schemas.clause_mapping import ClassificationStatus, ClausePolicy
from src.schemas.general_review import (
    GeneralReviewRequest,
    GeneralReviewResponse,
    Suggestion,
    TextInformation,
)
from src.services.clause_mapping.classifier import classify_clause
from src.services.clause_mapping.policy_store import get_clause_policy_store

logger = get_logger(__name__)

# Caps parallel Bedrock calls so large documents don't exhaust the client pool.
MAX_CONCURRENT_CALLS = 6

# Parallel review votes per batch; a finding needs a majority to survive. Raise to 3 for
# stronger run-to-run consistency at ~3x the per-review cost.
CONSENSUS_VOTES = 1
_MAJORITY = CONSENSUS_VOTES // 2 + 1

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

# Leading words of a clause quoted in its policy entry, so the model can tell
# which clause each legal-team policy belongs to.
_POLICY_ANCHOR_WORDS = 15

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "general_review"
_GENERAL_REVIEW_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_GENERAL_REVIEW_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# Identical documents always return the identical review (LRU, per process).
_CACHE_MAX_ENTRIES = 32
_response_cache: "OrderedDict[str, GeneralReviewResponse]" = OrderedDict()


def _request_fingerprint(request: GeneralReviewRequest) -> str:
    payload = json.dumps(
        {
            "paragraphs": [[para.paraindetifier, para.text] for para in request.textinformation],
            "context": [request.party_represented, request.review_objective, request.specific_concerns, request.agreement_type],
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
    for para in paragraphs:
        if not para.text.strip():
            continue
        own = _clause_title(para.text)
        if own:
            current = own
        if current:
            titles[para.paraindetifier] = current
    return titles


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


def _format_document(batch: _Batch) -> str:
    """Render a batch as plain paragraphs — identifiers stay out of the prompt."""

    return "\n\n".join(para.text.strip() for _, para in batch)


# A mapped clause: how confidently it was typed, and the policies that came with it.
_MappedClause = Tuple[ClassificationStatus, List[ClausePolicy]]


def _build_clause_units(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> List[List[int]]:
    """Group paragraph positions into clauses for vector mapping.

    Consecutive paragraphs under the same clause title form one clause; a
    paragraph with no title stands alone.
    """

    units: List[List[int]] = []
    previous: Optional[str] = None
    for idx, para in enumerate(paragraphs):
        if not para.text.strip():
            continue
        title = titles.get(para.paraindetifier)
        if units and title is not None and title == previous:
            units[-1].append(idx)
        else:
            units.append([idx])
        previous = title
    return units


def _describe_policy(policy: ClausePolicy) -> List[str]:
    """Render one policy as prompt lines, leaving out fields the legal team has not filled in yet."""

    label = f"{policy.canonical_type} — {policy.sub_type}" if policy.sub_type else policy.canonical_type
    lines = [f"{label} (inherent risk tier: {policy.inherent_risk_tier})"]
    if policy.acceptable_standard:
        lines.append(f"Acceptable standard: {policy.acceptable_standard}")
    if policy.unacceptable_triggers:
        lines.append("Unacceptable triggers: " + "; ".join(policy.unacceptable_triggers))
    if policy.fallback_guidelines:
        lines.append(f"Fallback position: {policy.fallback_guidelines}")
    return lines


def _render_policy_block(batch: _Batch, mapped: Dict[int, _MappedClause], unit_of: Dict[int, int]) -> str:
    """Render the policy entries for the clauses in one batch, in document order."""

    entries: List[str] = []
    rendered: Set[int] = set()
    for idx, para in batch:
        unit = unit_of.get(idx)
        if unit is None or unit in rendered:
            continue
        rendered.add(unit)
        status, policies = mapped[unit]

        words = para.text.split()
        anchor = " ".join(words[:_POLICY_ANCHOR_WORDS]) + ("..." if len(words) > _POLICY_ANCHOR_WORDS else "")
        lines = [f'Clause beginning "{anchor}"']
        if status is ClassificationStatus.NEEDS_CONFIRMATION:
            lines.append("Possible clause types (apply only the one that fits this clause; if none fits, review it without a policy):")
        elif len(policies) > 1:
            lines.append("Clause types (the clause covers several topics; apply every policy below):")
        else:
            lines.append("Clause type:")
        for policy in policies:
            first, *rest = _describe_policy(policy)
            lines.append(f"- {first}")
            lines.extend(f"  {line}" for line in rest)
        entries.append("\n".join(lines))

    if not entries:
        return ""
    return "Legal team policies for clauses in the text below (apply each only to the clause it names):\n\n" + "\n\n".join(entries) + "\n\n"


async def _build_policy_contexts(request: GeneralReviewRequest, batches: List[Tuple[_Batch, str]], titles: Dict[str, str], session_id: str) -> List[str]:
    """Map each clause to the legal team's policies and render one policy block per batch.

    Clauses go to the shared vector DB, classify_clause applies the score
    thresholds, and each accepted or candidate clause type brings its policy.
    Returns empty blocks — the policy-free review — when no clause search is
    configured or the search fails, so the vector DB never blocks a review.
    """

    empty = [""] * len(batches)
    search = get_clause_search()
    if search is None:
        return empty

    units = _build_clause_units(request.textinformation, titles)
    texts = ["\n\n".join(request.textinformation[idx].text.strip() for idx in unit) for unit in units]
    try:
        hits = await search.search(texts, request.agreement_type, session_id)
    except Exception:
        logger.exception("Clause search failed; reviewing without clause policies.")
        return empty
    if len(hits) != len(texts):
        logger.warning("Clause search returned %d result list(s) for %d clause(s); reviewing without clause policies.", len(hits), len(texts))
        return empty

    store = get_clause_policy_store()
    mapped: Dict[int, _MappedClause] = {}
    unit_of: Dict[int, int] = {}
    status_counts: Dict[str, int] = {status.value: 0 for status in ClassificationStatus}
    for unit, unit_hits in zip(units, hits):
        classification = classify_clause(unit_hits)
        status_counts[classification.status.value] += 1
        policies: List[ClausePolicy] = []
        for match in classification.matches:
            policy = store.get(match.canonical_type, match.sub_type, request.agreement_type)
            if policy is not None:
                policies.append(policy)
        if not policies:
            continue
        # A clause is keyed by its first paragraph position.
        mapped[unit[0]] = (classification.status, policies)
        for idx in unit:
            unit_of[idx] = unit[0]

    logger.info("Clause mapping: %d of %d clause(s) mapped to legal-team policies (%s).", len(mapped), len(units), ", ".join(f"{status}={count}" for status, count in status_counts.items()))
    return [_render_policy_block(batch, mapped, unit_of) for batch, _ in batches]


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
        return para.text, suggestion.suggested_fix

    span = para.text.find(exact_text)
    prefix = para.text[:span]
    suffix = para.text[span + len(exact_text):]

    # If the model already returned a full-paragraph rewrite, splicing would duplicate text.
    fix = suggestion.suggested_fix
    already_full = (len(prefix.strip()) < 20 or prefix.strip()[:30] in fix) and (len(suffix.strip()) < 20 or suffix.strip()[-30:] in fix)
    full_fix = fix if already_full else prefix + fix + suffix
    return para.text, full_fix


def _validate_batch(suggestions: List[Suggestion], batch: _Batch, seen_positions: Set[int], titles: Dict[str, str]) -> List[Tuple[int, Suggestion]]:
    """Ground each suggestion to one paragraph, expand it to the full paragraph, and attach its id and clause title."""

    valid: List[Tuple[int, Suggestion]] = []
    for suggestion in suggestions:
        grounded = _ground(suggestion.original_text, batch)
        if grounded is None:
            logger.warning("Dropping suggestion for clause '%s' — original_text could not be grounded in a single source paragraph.", suggestion.clause)
            continue
        position, para, exact_text = grounded
        if position in seen_positions:
            continue
        seen_positions.add(position)
        original, full_fix = _expand_to_paragraph(suggestion, para, exact_text)
        # The document's own clause title keeps names identical across whole-doc
        # and selection runs; the model's label only fills in when the payload
        # carries no heading for this paragraph.
        clause = titles.get(para.paraindetifier, suggestion.clause)
        valid.append((position, suggestion.model_copy(update={"clause": clause, "original_text": original, "suggested_fix": full_fix, "para_identifier": para.paraindetifier})))

    valid.sort(key=lambda pair: pair[0])
    return valid


async def _review_batch(batch: _Batch, context: str, reviewer_context: str, policy_context: str, semaphore: asyncio.Semaphore, session_id: str) -> GeneralReviewResponse:
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
                    context={"document": document, "reviewer_context": reviewer_context, "policy_context": policy_context},
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


async def _review_batch_consensus(
    batch: _Batch, context: str, reviewer_context: str, policy_context: str, semaphore: asyncio.Semaphore, session_id: str, titles: Dict[str, str]
) -> List[Tuple[int, Suggestion]]:
    """Review one batch with parallel votes and keep only majority-backed findings."""

    outcomes = await asyncio.gather(*[_review_batch(batch, context, reviewer_context, policy_context, semaphore, session_id) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)

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
    for grounded in per_vote:
        for position, suggestion in grounded.items():
            if counts[position] >= _MAJORITY and position not in picked:
                picked[position] = suggestion

    return sorted(picked.items())


async def general_review_service(request: GeneralReviewRequest, session_id: str) -> GeneralReviewResponse:
    """Review the full document or the selected clauses the frontend sent."""

    fingerprint = _request_fingerprint(request)
    cached = _cache_get(fingerprint)
    if cached is not None:
        logger.info("General review: cache hit — returning the stored review for this document.")
        return cached

    batches = _build_batches(request.textinformation)
    if not batches:
        return GeneralReviewResponse(suggestions=[])

    reviewer_context = _build_reviewer_context(request)
    titles = _build_clause_titles(request.textinformation)
    policy_contexts = await _build_policy_contexts(request, batches, titles, session_id)
    logger.info("General review: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else "")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    results = await asyncio.gather(
        *[_review_batch_consensus(batch, context, reviewer_context, policy_context, semaphore, session_id, titles) for (batch, context), policy_context in zip(batches, policy_contexts)]
    )

    seen_positions: Set[int] = set()
    positioned: List[Tuple[int, Suggestion]] = []
    for batch_result in results:
        for position, suggestion in batch_result:
            if position in seen_positions:
                continue
            seen_positions.add(position)
            positioned.append((position, suggestion))

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

        batches = _build_batches(request.textinformation)
        reviewer_context = _build_reviewer_context(request)
        titles = _build_clause_titles(request.textinformation)
        policy_contexts = await _build_policy_contexts(request, batches, titles, session_id)
        logger.info("General review stream: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else "")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [
            asyncio.create_task(_review_batch_consensus(batch, context, reviewer_context, policy_context, semaphore, session_id, titles))
            for (batch, context), policy_context in zip(batches, policy_contexts)
        ]

        yield frame('{"suggestions": [')

        emitted: List[Suggestion] = []
        seen_positions: Set[int] = set()
        for task in tasks:
            for position, suggestion in await task:
                if position in seen_positions:
                    continue
                seen_positions.add(position)
                separator = "" if not emitted else ","
                yield frame(separator + suggestion.model_dump_json())
                emitted.append(suggestion)

        yield frame("]}")
        _cache_put(fingerprint, GeneralReviewResponse(suggestions=emitted))

    except Exception as exc:
        logger.exception("General review stream failed: %s", exc)
        for task in tasks:
            task.cancel()
        yield f"data: {json.dumps({'error': 'General review failed. Please try again.'})}\n\n"

    yield "data: [DONE]\n\n"
