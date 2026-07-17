import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple

from src.config.logging import get_logger
from src.core.container import get_bedrock_model
from src.schemas.general_review import (
    GeneralReviewRequest,
    GeneralReviewResponse,
    Suggestion,
    TextInformation,
)

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
            "context": [request.party_represented, request.review_objective, request.specific_concerns],
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


def _validate_batch(suggestions: List[Suggestion], batch: _Batch, seen_positions: Set[int]) -> List[Tuple[int, Suggestion]]:
    """Ground each suggestion to one paragraph, expand it to the full paragraph, and attach its id."""

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
        valid.append((position, suggestion.model_copy(update={"original_text": original, "suggested_fix": full_fix, "para_identifier": para.paraindetifier})))

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


async def _review_batch_consensus(batch: _Batch, context: str, reviewer_context: str, semaphore: asyncio.Semaphore, session_id: str) -> List[Tuple[int, Suggestion]]:
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
        for position, suggestion in _validate_batch(vote.suggestions, batch, set()):
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
    logger.info("General review: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else "")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    results = await asyncio.gather(*[_review_batch_consensus(batch, context, reviewer_context, semaphore, session_id) for batch, context in batches])

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
        logger.info("General review stream: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else "")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [asyncio.create_task(_review_batch_consensus(batch, context, reviewer_context, semaphore, session_id)) for batch, context in batches]

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
