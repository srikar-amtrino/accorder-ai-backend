import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple, cast

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_clause_search
from src.schemas.clause_mapping import ClauseMatch, ClausePolicy
from src.schemas.general_review import (
    GeneralReviewFindings,
    GeneralReviewRequest,
    GeneralReviewResponse,
    ReviewFinding,
    Suggestion,
    TextInformation,
)
from src.services.clause_mapping.policy_store import get_clause_policy_store
from src.services.clause_mapping.resilient_classifier import (
    HEADING_MATCH_CONFIDENCE,
    LAYER1_HEADING,
    LAYER2_QDRANT,
    LAYER3_FALLBACK,
    MIN_CLAUSE_WORDS,
    SKIPPED_SHORT,
    UNCLASSIFIED,
    ResolvedClause,
    classify_clause_resilient,
    classify_layer1_heading,
)
from src.services.clause_mapping.search import BaseClauseSearch
from src.services.general_review.clause_cache import NO_FINDING, ClauseAssessment, ZDRClauseHashCache, clause_cache
from src.services.general_review.risk_matrix import RiskLevel, compute_deterministic_risk_level, tier_for_unmapped
from src.services.general_review.segmentation import (
    TITLE_STOP_WORDS,
    build_clause_titles,
    build_smart_embedding_query,
    detect_clause_start,
    extract_defined_terms,
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

# Read-only context carried into the next batch when a forced cut is unavoidable.
CONTEXT_TAIL_CHARS = 1200

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
        document_text = " ".join(para.text for para in request.textinformation).casefold()
        if request.party_represented.casefold() not in document_text:
            logger.warning("Party represented %r does not appear in the document; the model will have to infer which party is the client.", request.party_represented)
    if request.review_objective:
        lines.append(f"Primary objective for this review: {request.review_objective}")
    if request.specific_concerns:
        lines.append(f"Specific concerns to cover: {request.specific_concerns}")
    if not lines:
        return ""

    joined = "\n".join(lines)
    return "Reviewer context (the client's stated perspective and priorities — apply it per the review instructions; " "it never overrides them):\n\n" f"{joined}\n\n"


# Clause headings listed with every batch of a multi-batch review, so a batch
# never calls a provision missing that another batch contains.
_OUTLINE_MAX_ENTRIES = 120


def _build_outline_context(paragraphs: Sequence[TextInformation], titles: Dict[str, str], batch_count: int) -> str:
    """Render the document's clause headings, in order, and its defined terms as the prompt's outline block.

    Empty for a single-batch review, where the model already sees the whole text.
    """

    if batch_count < 2:
        return ""
    seen: Set[str] = set()
    headings: List[str] = []
    for para in paragraphs:
        title = titles.get(para.paraindetifier)
        if title and title not in seen:
            seen.add(title)
            headings.append(title)
    if not headings:
        return ""
    listed = "\n".join(f"- {heading}" for heading in headings[:_OUTLINE_MAX_ENTRIES])
    block = "Document outline (headings of every clause in the whole document, in order — for orientation only; review only the text below):\n\n" f"{listed}\n\n"
    terms = extract_defined_terms(paragraphs)
    if terms:
        block += "Defined terms (terms the whole document defines — for orientation only):\n\n" + ", ".join(f'"{term}"' for term in terms) + "\n\n"
    return block


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


def _is_clause_start(text: str) -> bool:
    """A paragraph where a new clause begins — the only safe place to cut a batch."""

    return detect_clause_start(text)[0]


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
    (heading, inline lead-in, or numbered paragraph), so a clause's paragraphs
    always stay together. If a document has no detectable clause starts, the
    hard limit forces a cut and the next batch carries the previous tail as
    read-only context so nothing is judged without its surroundings.
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


@dataclass
class BatchPolicies:
    """One batch's legal-team policies: the prompt block, and the policy behind each paragraph position."""

    block: str = ""
    policy_of: Dict[int, ClausePolicy] = field(default_factory=dict)


_NO_POLICIES = BatchPolicies()


def _build_clause_units(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> List[List[int]]:
    """Group paragraph positions into clauses for clause-type mapping.

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


def _clause_type_label(canonical_type: str) -> str:
    """Readable name for a clause type slug: 'fees-and-payment' -> 'Fees and Payment'."""

    words = canonical_type.replace("-", " ").replace("_", " ").split()
    return " ".join(word if word in TITLE_STOP_WORDS else word.capitalize() for word in words)


def _describe_policy(policy: ClausePolicy) -> List[str]:
    """Render one policy as prompt lines, leaving out fields the legal team has not filled in yet."""

    type_label = _clause_type_label(policy.canonical_type)
    label = f"{type_label} — {policy.sub_type}" if policy.sub_type else type_label
    lines = [f"{label} (inherent risk tier: {policy.inherent_risk_tier})"]
    if policy.acceptable_standard:
        lines.append(f"Acceptable standard: {policy.acceptable_standard}")
    if policy.unacceptable_triggers:
        lines.append("Unacceptable triggers (copy any that the clause's wording triggers, verbatim, into triggered_unacceptable_flags):")
        lines.extend(f"  * {trigger}" for trigger in policy.unacceptable_triggers)
    if policy.fallback_guidelines:
        lines.append(f"Fallback position: {policy.fallback_guidelines}")
    return lines


def _render_policy_block(batch: _Batch, mapped: Dict[int, ClausePolicy]) -> str:
    """Render the policy entries for the clauses in one batch, in document order.

    mapped is keyed by a clause's first paragraph position.
    """

    entries: List[str] = []
    for idx, para in batch:
        policy = mapped.get(idx)
        if policy is None:
            continue
        words = para.text.split()
        anchor = " ".join(words[:_POLICY_ANCHOR_WORDS]) + ("..." if len(words) > _POLICY_ANCHOR_WORDS else "")
        first, *rest = _describe_policy(policy)
        lines = [f'Clause beginning "{anchor}"', "Clause type:", f"- {first}"]
        lines.extend(f"  {line}" for line in rest)
        entries.append("\n".join(lines))

    if not entries:
        return ""
    return "Legal team policies for clauses in the text below (apply each only to the clause it names):\n\n" + "\n\n".join(entries) + "\n\n"


# Total time the vector path (embedding + Qdrant) may spend per review. Past
# this budget the remaining clauses are classified offline (layers 1 and 3).
POLICY_MAPPING_BUDGET_SECONDS = 240.0

# One policy set per batch, resolved as that batch's clauses are mapped.
_PolicyContexts = List["asyncio.Future[BatchPolicies]"]


def _start_policy_mapping(request: GeneralReviewRequest, batches: List[Tuple[_Batch, str]], titles: Dict[str, str], session_id: str) -> Tuple[_PolicyContexts, "asyncio.Task[None]"]:
    """Begin mapping clauses to the legal team's policies, one batch at a time, ahead of the review.

    Returns a future per batch that resolves to that batch's policies, and
    the background task doing the mapping. Each batch's review awaits only
    its own policies, so the first LLM call starts after the first batch's
    clauses are mapped rather than after the whole document is. The mapping
    never blocks or fails a review: when the vector path is unavailable the
    offline classifier still supplies every clause it can name a type for.
    """

    loop = asyncio.get_running_loop()
    contexts: _PolicyContexts = [loop.create_future() for _ in batches]
    task = asyncio.create_task(_map_policies(get_clause_search(), request, batches, titles, session_id, contexts))
    return contexts, task


@dataclass
class _ClauseQuery:
    """A clause unit prepared for classification."""

    unit: List[int]
    heading: str
    query: str
    word_count: int
    needs_vector: bool


async def _map_policies(
    search: Optional[BaseClauseSearch], request: GeneralReviewRequest, batches: List[Tuple[_Batch, str]], titles: Dict[str, str], session_id: str, contexts: _PolicyContexts
) -> None:
    """Map each batch's clauses to one clause type each, in document order, and resolve its policies.

    Layer 1 locks clauses whose heading names their type without any network
    call. The rest of a batch's clauses go to the vector DB in one request;
    if that path is disabled, fails, or exhausts its budget, every remaining
    clause is classified offline instead, so a policy block is still rendered.
    """

    paragraphs = request.textinformation
    units = _build_clause_units(paragraphs, titles)
    # A clause belongs to the batch holding its first paragraph.
    batch_of: Dict[int, int] = {idx: number for number, (batch, _) in enumerate(batches) for idx, _ in batch}
    batch_units: List[List[List[int]]] = [[] for _ in batches]
    for unit in units:
        number = batch_of.get(unit[0])
        if number is not None:
            batch_units[number].append(unit)

    store = get_clause_policy_store()
    mapped: Dict[int, ClausePolicy] = {}
    source_counts: Counter = Counter()
    vector_available = search is not None
    started = asyncio.get_running_loop().time()
    completed = 0
    try:
        for number, ((batch, _), units_here) in enumerate(zip(batches, batch_units)):
            prepared: List[_ClauseQuery] = []
            for unit in units_here:
                texts = [paragraphs[idx].text.strip() for idx in unit]
                heading = titles.get(paragraphs[unit[0]].paraindetifier, "")
                query = build_smart_embedding_query(heading, texts)
                word_count = len(" ".join(texts).split())
                layer1 = classify_layer1_heading(heading, query)
                locked = bool(layer1 and layer1[1] >= HEADING_MATCH_CONFIDENCE)
                prepared.append(_ClauseQuery(unit, heading, query, word_count, needs_vector=word_count >= MIN_CLAUSE_WORDS and not locked))

            hits_of: Dict[int, List[ClauseMatch]] = {}
            to_search = [clause for clause in prepared if clause.needs_vector]
            if to_search and vector_available and search is not None:
                if asyncio.get_running_loop().time() - started > POLICY_MAPPING_BUDGET_SECONDS:
                    vector_available = False
                    logger.warning("Clause mapping budget of %.0fs spent after %d of %d batch(es); classifying the remaining clauses offline.", POLICY_MAPPING_BUDGET_SECONDS, number, len(batches))
                else:
                    try:
                        hits = await search.search([clause.query for clause in to_search], request.agreement_type, session_id)
                        if len(hits) != len(to_search):
                            raise RuntimeError(f"clause search returned {len(hits)} result list(s) for {len(to_search)} clause(s)")
                        for clause, clause_hits in zip(to_search, hits):
                            hits_of[clause.unit[0]] = clause_hits
                    except Exception:
                        vector_available = False
                        logger.exception("Clause search failed on batch %d of %d; classifying the remaining clauses offline.", number + 1, len(batches))

            policy_of: Dict[int, ClausePolicy] = {}
            for clause in prepared:
                resolved = classify_clause_resilient(clause.heading, clause.query, clause.word_count, hits_of.get(clause.unit[0]) if vector_available else None)
                source_counts[resolved.source] += 1
                policy = store.get(resolved.canonical_type, None, request.agreement_type) if resolved.canonical_type else None
                if policy is None:
                    continue
                _log_resolution(session_id, clause, resolved)
                mapped[clause.unit[0]] = policy
                for idx in clause.unit:
                    policy_of[idx] = policy

            contexts[number].set_result(BatchPolicies(_render_policy_block(batch, mapped), policy_of))
            completed = number + 1
    except Exception:
        logger.exception("Clause mapping failed after %d of %d batch(es); reviewing the rest without clause policies.", completed, len(batches))
    finally:
        for context in contexts:
            if not context.done():
                context.set_result(_NO_POLICIES)
        logger.info(
            "Clause mapping: %d of %d clause(s) mapped to legal-team policies across %d of %d batch(es) in %.1fs (%s).",
            len(mapped),
            sum(source_counts.values()),
            completed,
            len(batches),
            asyncio.get_running_loop().time() - started,
            ", ".join(f"{source}={source_counts[source]}" for source in (LAYER1_HEADING, LAYER2_QDRANT, LAYER3_FALLBACK, SKIPPED_SHORT, UNCLASSIFIED)),
        )


def _log_resolution(session_id: str, clause: _ClauseQuery, resolved: ResolvedClause) -> None:
    logger.debug(
        "Clause resolved.",
        session_id=session_id,
        clause=" ".join(clause.query.split()[:8]),
        canonical_type=resolved.canonical_type,
        confidence=resolved.confidence,
        source=resolved.source,
    )


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


def _expand_to_paragraph(suggested_fix: str, para: TextInformation, exact_text: str) -> Tuple[str, str]:
    """Return (original, fix) covering the complete paragraph, splicing a partial fix if needed."""

    if exact_text.strip() == para.text.strip():
        return para.text, suggested_fix

    span = para.text.find(exact_text)
    prefix = para.text[:span]
    suffix = para.text[span + len(exact_text) :]

    # If the model already returned a full-paragraph rewrite, splicing would duplicate text.
    already_full = (len(prefix.strip()) < 20 or prefix.strip()[:30] in suggested_fix) and (len(suffix.strip()) < 20 or suffix.strip()[-30:] in suggested_fix)
    full_fix = suggested_fix if already_full else prefix + suggested_fix + suffix
    return para.text, full_fix


def _fold_trigger(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".")


def _matching_triggers(reported: Sequence[str], policy: Optional[ClausePolicy], clause: str) -> List[str]:
    """Keep only the reported triggers that exist, verbatim, in the clause's policy.

    The model is asked to copy triggers exactly; anything else (a paraphrase,
    or a trigger for a clause with no policy) carries no weight in the risk
    computation, so the same wording cannot score differently run to run.
    """

    if not reported:
        return []
    if policy is None:
        logger.warning("Dropping %d trigger flag(s) for clause '%s' — the clause has no legal-team policy.", len(reported), clause)
        return []
    known = {_fold_trigger(trigger): trigger for trigger in policy.unacceptable_triggers}
    kept: List[str] = []
    for flag in reported:
        trigger = known.get(_fold_trigger(flag))
        if trigger is None:
            logger.warning("Dropping trigger flag %r for clause '%s' — not a trigger of its %s policy.", flag, clause, policy.canonical_type)
        elif trigger not in kept:
            kept.append(trigger)
    return kept


def _risk_level(finding: ReviewFinding, policy: Optional[ClausePolicy]) -> RiskLevel:
    """Compute the finding's risk level in code from its structured facts and the clause type's inherent tier."""

    flags = _matching_triggers(finding.triggered_unacceptable_flags, policy, finding.clause)
    tier = policy.inherent_risk_tier if policy is not None else tier_for_unmapped(finding.severity_category)
    return compute_deterministic_risk_level(tier, flags, finding.falls_short_of_acceptable_standard, finding.is_materially_one_sided_against_client, finding.severity_category)


def _validate_batch(findings: List[ReviewFinding], batch: _Batch, seen_positions: Set[int], titles: Dict[str, str], policies: BatchPolicies) -> List[Tuple[int, Suggestion]]:
    """Ground each finding to one paragraph, expand it to the full paragraph, compute its risk level, and build the suggestion."""

    valid: List[Tuple[int, Suggestion]] = []
    for finding in findings:
        grounded = _ground(finding.original_text, batch)
        if grounded is None:
            logger.warning("Dropping finding for clause '%s' — original_text could not be grounded in a single source paragraph.", finding.clause)
            continue
        position, para, exact_text = grounded
        if position in seen_positions:
            continue
        seen_positions.add(position)
        original, full_fix = _expand_to_paragraph(finding.suggested_fix, para, exact_text)
        # The document's own clause title keeps names identical across whole-doc
        # and selection runs; the model's label only fills in when the payload
        # carries no heading for this paragraph.
        clause = titles.get(para.paraindetifier, finding.clause)
        risk_level = _risk_level(finding, policies.policy_of.get(position))
        valid.append((position, Suggestion(clause=clause, reason=finding.reason, risk_level=risk_level, original_text=original, suggested_fix=full_fix, para_identifier=para.paraindetifier)))

    valid.sort(key=lambda pair: pair[0])
    return valid


async def _review_batch(batch: _Batch, context: str, reviewer_context: str, outline_context: str, policy_context: str, semaphore: asyncio.Semaphore, session_id: str) -> GeneralReviewFindings:
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
                    context={"document": document, "reviewer_context": reviewer_context, "outline_context": outline_context, "policy_context": policy_context},
                    response_model=GeneralReviewFindings,
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


# Paragraph position -> clause-cache key, computed once per review.
_CacheKeys = Dict[int, str]


def _cache_keys(request: GeneralReviewRequest, titles: Dict[str, str], reviewer_context: str, cache: ZDRClauseHashCache) -> _CacheKeys:
    """Key every paragraph by its own text, clause title, and the reviewer's context."""

    return {
        idx: cache.compute_key(para.text, titles.get(para.paraindetifier, ""), request.party_represented, request.agreement_type, reviewer_context)
        for idx, para in enumerate(request.textinformation)
        if para.text.strip()
    }


def _replay(para: TextInformation, cached: ClauseAssessment) -> Suggestion:
    return Suggestion(
        clause=cached.clause, reason=cached.reason, risk_level=cast(RiskLevel, cached.risk_level), original_text=para.text, suggested_fix=cached.suggested_fix, para_identifier=para.paraindetifier
    )


async def _review_batch_consensus(
    batch: _Batch,
    context: str,
    reviewer_context: str,
    outline_context: str,
    policy_context: "asyncio.Future[BatchPolicies]",
    semaphore: asyncio.Semaphore,
    session_id: str,
    titles: Dict[str, str],
    cache_keys: _CacheKeys,
    cache: ZDRClauseHashCache = clause_cache,
) -> List[Tuple[int, Suggestion]]:
    """Review one batch, replaying cached outcomes for unchanged paragraphs and keeping only majority-backed findings.

    A paragraph reviewed before (same text, clause title, and reviewer
    context, under the same policies and prompts) keeps its earlier outcome,
    so an unchanged clause never changes its suggestion or risk level between
    runs. When every paragraph of the batch is cached, no model call is made.
    """

    cached: Dict[int, Optional[ClauseAssessment]] = {position: cache.get(cache_keys[position]) for position, _ in batch}
    if all(assessment is not None for assessment in cached.values()):
        logger.info("Batch of %d paragraph(s) replayed from the clause cache.", len(batch))
        return [(position, _replay(para, assessment)) for position, para in batch for assessment in (cached[position],) if assessment is not None and assessment.flagged]

    # Wait for this batch's clauses to be mapped before taking a model slot.
    policies = await policy_context
    outcomes = await asyncio.gather(*[_review_batch(batch, context, reviewer_context, outline_context, policies.block, semaphore, session_id) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)

    # A majority of votes must succeed; one lost vote never fails the review.
    votes = [outcome for outcome in outcomes if isinstance(outcome, GeneralReviewFindings)]
    if len(votes) < _MAJORITY:
        raise next(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    if len(votes) < CONSENSUS_VOTES:
        logger.warning("Batch consensus proceeding with %d of %d votes.", len(votes), CONSENSUS_VOTES)

    # One grounded finding per paragraph per vote, so vote counts stay honest.
    per_vote: List[Dict[int, Suggestion]] = []
    for vote in votes:
        grounded: Dict[int, Suggestion] = {}
        for position, suggestion in _validate_batch(vote.findings, batch, set(), titles, policies):
            grounded.setdefault(position, suggestion)
        per_vote.append(grounded)

    counts = Counter(position for grounded in per_vote for position in grounded)
    picked: Dict[int, Suggestion] = {}
    for grounded in per_vote:
        for position, suggestion in grounded.items():
            if counts[position] >= _MAJORITY and position not in picked:
                picked[position] = suggestion

    # Cached outcomes win for paragraphs reviewed before; fresh ones are remembered.
    result: Dict[int, Suggestion] = {}
    replayed = 0
    for position, para in batch:
        assessment = cached[position]
        if assessment is not None:
            replayed += 1
            if assessment.flagged:
                result[position] = _replay(para, assessment)
            continue
        fresh = picked.get(position)
        if fresh is None:
            cache.set(cache_keys[position], NO_FINDING)
        else:
            cache.set(cache_keys[position], ClauseAssessment(True, fresh.clause, fresh.risk_level, fresh.reason, fresh.suggested_fix))
            result[position] = fresh
    if replayed:
        logger.info("Batch: %d of %d paragraph(s) replayed from the clause cache.", replayed, len(batch))

    return sorted(result.items())


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
    titles = build_clause_titles(request.textinformation)
    outline_context = _build_outline_context(request.textinformation, titles, len(batches))
    cache_keys = _cache_keys(request, titles, reviewer_context, clause_cache)
    policy_contexts, mapping = _start_policy_mapping(request, batches, titles, session_id)
    logger.info(
        "General review: %d paragraph(s) split into %d batch(es), %d votes each%s.", len(request.textinformation), len(batches), CONSENSUS_VOTES, ", with reviewer context" if reviewer_context else ""
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    try:
        results = await asyncio.gather(
            *[
                _review_batch_consensus(batch, context, reviewer_context, outline_context, policy_context, semaphore, session_id, titles, cache_keys)
                for (batch, context), policy_context in zip(batches, policy_contexts)
            ]
        )
    finally:
        mapping.cancel()

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
    mapping: Optional["asyncio.Task[None]"] = None
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
        titles = build_clause_titles(request.textinformation)
        outline_context = _build_outline_context(request.textinformation, titles, len(batches))
        cache_keys = _cache_keys(request, titles, reviewer_context, clause_cache)
        policy_contexts, mapping = _start_policy_mapping(request, batches, titles, session_id)
        logger.info(
            "General review stream: %d paragraph(s) split into %d batch(es), %d votes each%s.",
            len(request.textinformation),
            len(batches),
            CONSENSUS_VOTES,
            ", with reviewer context" if reviewer_context else "",
        )

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [
            asyncio.create_task(_review_batch_consensus(batch, context, reviewer_context, outline_context, policy_context, semaphore, session_id, titles, cache_keys))
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
        if mapping is not None:
            mapping.cancel()
        yield f"data: {json.dumps({'error': 'General review failed. Please try again.'})}\n\n"

    yield "data: [DONE]\n\n"
