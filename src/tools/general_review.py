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

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "general_review"
_GENERAL_REVIEW_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_GENERAL_REVIEW_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# Identical documents always return the identical review (LRU, per process).
_CACHE_MAX_ENTRIES = 32
_response_cache: "OrderedDict[str, GeneralReviewResponse]" = OrderedDict()


def _request_fingerprint(paragraphs: Sequence[TextInformation]) -> str:
    payload = json.dumps([[para.paraindetifier, para.text] for para in paragraphs], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _build_batches(paragraphs: Sequence[TextInformation]) -> List[_Batch]:
    """Split paragraphs into document-ordered batches, breaking only at clause boundaries.

    Once a batch exceeds the char budget it closes at the next heading, so a
    clause's paragraphs always stay together in one review call. The hard limit
    guards documents with no detectable headings.
    """

    indexed = [(idx, para) for idx, para in enumerate(paragraphs) if para.text.strip()]

    batches: List[_Batch] = []
    current: _Batch = []
    current_size = 0

    for item in indexed:
        over_budget = current_size >= BATCH_CHAR_BUDGET and _is_heading(item[1].text)
        if current and (over_budget or current_size >= BATCH_HARD_LIMIT):
            batches.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += len(item[1].text)

    if current:
        batches.append(current)
    return batches


def _format_document(batch: _Batch) -> str:
    """Render a batch as plain paragraphs — identifiers stay out of the prompt."""

    return "\n\n".join(para.text.strip() for _, para in batch)


_WS_RE = re.compile(r"\s+")

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


def _ground(original_text: str, batch: _Batch) -> Optional[Tuple[int, str]]:
    """Find original_text inside a single paragraph and return (position, exact source text).

    Exact match first; then a whitespace/quote-tolerant match that recovers the
    true verbatim substring, so the returned text always applies cleanly.
    """

    needle = original_text.strip()
    if not needle:
        return None

    for idx, para in batch:
        if needle in para.text:
            return idx, needle

    pattern = _tolerant_pattern(needle)
    for idx, para in batch:
        match = re.search(pattern, para.text)
        if match:
            return idx, match.group(0)
    return None


def _validate_batch(suggestions: List[Suggestion], batch: _Batch, all_texts: Sequence[str], seen: Set[str]) -> List[Tuple[int, Suggestion]]:
    """Keep only verbatim-grounded, unambiguous, unseen suggestions, ordered by document position."""

    valid: List[Tuple[int, Suggestion]] = []
    for suggestion in suggestions:
        grounded = _ground(suggestion.original_text, batch)
        if grounded is None:
            logger.warning("Dropping suggestion for clause '%s' — original_text could not be grounded in a single source paragraph.", suggestion.clause)
            continue
        position, exact_text = grounded
        # An anchor found in more than one paragraph could be applied to the wrong place.
        if sum(1 for text in all_texts if exact_text in text) > 1:
            logger.warning("Dropping suggestion for clause '%s' — original_text is ambiguous (appears in multiple paragraphs).", suggestion.clause)
            continue
        key = _WS_RE.sub(" ", exact_text).strip()
        if key in seen:
            continue
        seen.add(key)
        if exact_text != suggestion.original_text:
            suggestion = suggestion.model_copy(update={"original_text": exact_text})
        valid.append((position, suggestion))

    valid.sort(key=lambda pair: pair[0])
    return valid


async def _review_batch(batch: _Batch, semaphore: asyncio.Semaphore, session_id: str) -> GeneralReviewResponse:
    """Run the schema-enforced review call for one batch, with a single retry."""

    llm_model = get_bedrock_model()
    document = _format_document(batch)

    async with semaphore:
        for attempt in (1, 2):
            try:
                return await llm_model.generate(
                    prompt=_GENERAL_REVIEW_USER,
                    context={"document": document},
                    response_model=GeneralReviewResponse,
                    system_message=_GENERAL_REVIEW_SYSTEM,
                    session_id=session_id,
                )
            except Exception:
                if attempt == 2:
                    raise
                logger.exception("Batch review failed on attempt %d; retrying once.", attempt)
                await asyncio.sleep(2)

    raise RuntimeError("unreachable")


async def _review_batch_consensus(batch: _Batch, all_texts: Sequence[str], semaphore: asyncio.Semaphore, session_id: str) -> List[Tuple[int, Suggestion]]:
    """Review one batch with parallel votes and keep only majority-backed findings."""

    outcomes = await asyncio.gather(*[_review_batch(batch, semaphore, session_id) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)

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
        for position, suggestion in _validate_batch(vote.suggestions, batch, all_texts, set()):
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

    fingerprint = _request_fingerprint(request.textinformation)
    cached = _cache_get(fingerprint)
    if cached is not None:
        logger.info("General review: cache hit — returning the stored review for this document.")
        return cached

    batches = _build_batches(request.textinformation)
    if not batches:
        return GeneralReviewResponse(suggestions=[])

    logger.info("General review: %d paragraph(s) split into %d batch(es), %d votes each.", len(request.textinformation), len(batches), CONSENSUS_VOTES)

    all_texts = [para.text for para in request.textinformation]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    results = await asyncio.gather(*[_review_batch_consensus(batch, all_texts, semaphore, session_id) for batch in batches])

    seen: Set[str] = set()
    positioned: List[Tuple[int, Suggestion]] = []
    for batch_result in results:
        for position, suggestion in batch_result:
            key = _WS_RE.sub(" ", suggestion.original_text).strip()
            if key in seen:
                continue
            seen.add(key)
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
        fingerprint = _request_fingerprint(request.textinformation)
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
        logger.info("General review stream: %d paragraph(s) split into %d batch(es), %d votes each.", len(request.textinformation), len(batches), CONSENSUS_VOTES)

        all_texts = [para.text for para in request.textinformation]
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [asyncio.create_task(_review_batch_consensus(batch, all_texts, semaphore, session_id)) for batch in batches]

        yield frame('{"suggestions": [')

        emitted: List[Suggestion] = []
        seen: Set[str] = set()
        for task in tasks:
            for _, suggestion in await task:
                key = _WS_RE.sub(" ", suggestion.original_text).strip()
                if key in seen:
                    continue
                seen.add(key)
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
