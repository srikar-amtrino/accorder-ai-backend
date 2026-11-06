import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, cast

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_clause_search
from src.schemas.clause_mapping import ClauseMatch, ClausePolicy
from src.schemas.general_review import (
    CLAUSE_WIDE_OMISSION,
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
from src.services.general_review.document_facts import (
    DocumentFacts,
    apply_blank_field_guard,
    build_document_facts,
    claims_blank_field,
    is_expense_approval_trigger,
    restore_blank_fields,
)
from src.services.general_review.party_resolver import FAVORS_REPRESENTED, PartyContext, clause_direction, is_sole_mutuality_complaint, resolve_party_context
from src.services.general_review.risk_matrix import (
    MATERIAL_DEVIATION,
    MINOR_HYGIENE,
    RiskLevel,
    apply_gate_floor,
    compute_deterministic_risk_level,
    drop_direction_contradicted_flags,
    tier_for_unmapped,
)
from src.services.general_review.segmentation import (
    ROLE_BOILERPLATE,
    ROLE_CONTEXT_ONLY,
    ROLE_DEFINITION,
    ROLE_HEADING,
    ROLE_PRIMARY,
    ROLE_SECONDARY,
    TITLE_STOP_WORDS,
    ClauseStructure,
    build_clause_structure,
    build_clause_titles,
    build_clause_units,
    build_smart_embedding_query,
    detect_clause_start,
    extract_defined_terms,
    fold_sentence,
    format_with_role,
    is_clause_wide_topic,
    new_sentences,
    remove_sentence,
    sentence_keys,
    strip_role_tags,
)
from src.services.general_review.taxonomy_gates import criterion_id_of, strongest_gate

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


def _build_reviewer_context(request: GeneralReviewRequest, parties: Optional[PartyContext] = None) -> str:
    """Render the questionnaire answers as the prompt's reviewer-context block.

    Returns an empty string when the user skipped every field, which keeps the
    rendered prompt byte-identical to the questionnaire-less flow.

    When the represented party was resolved against the preamble, the client
    is named by the agreement's own defined term and the reviewer's shorthand
    for it is replaced wherever it appears, so the raw wording ("ACI") never
    reaches the model as something to interpret.
    """

    def worded(text: str) -> str:
        return parties.substitute(text) if parties is not None else text

    lines: List[str] = []
    if request.party_represented:
        if parties is not None and parties.resolved:
            lines.append(parties.client_line())
        else:
            lines.append(f"Party the client represents: {request.party_represented}")
            logger.warning("Party represented %r could not be matched to a party the agreement defines; the model will have to infer which party is the client.", request.party_represented)
    if request.review_objective:
        lines.append(f"Primary objective for this review: {worded(request.review_objective)}")
    if request.specific_concerns:
        lines.append(f"Specific concerns to cover: {worded(request.specific_concerns)}")
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


def _format_document(batch: _Batch, structure: Optional[ClauseStructure] = None, shown_text: Optional[Dict[int, str]] = None) -> str:
    """Render a batch as paragraphs, each led by its role tag — identifiers stay out of the prompt.

    shown_text holds the paragraphs the model is shown in normalised form (a
    blank field restored); every other paragraph is rendered as it was sent.
    """

    shown = shown_text or {}
    if structure is None:
        return "\n\n".join(shown.get(position, para.text).strip() for position, para in batch)
    return "\n\n".join(format_with_role(shown.get(position, para.text).strip(), structure.role(position)) for position, para in batch)


@dataclass(frozen=True)
class ReviewPlan:
    """Everything decided about a document in code before any model call.

    Built once per review from the paragraphs alone, so every batch — and
    every run — works from the same parties, facts, and paragraph roles.
    """

    titles: Dict[str, str]
    structure: ClauseStructure
    facts: DocumentFacts
    parties: Optional[PartyContext] = None
    reviewer_context: str = ""
    party_block: str = ""
    facts_block: str = ""
    outline_context: str = ""
    cache_keys: Dict[int, str] = field(default_factory=dict)
    # Position -> the text the model is shown, for the paragraphs normalised before the prompt is built.
    shown_text: Dict[int, str] = field(default_factory=dict)
    # Every sentence of the agreement, folded, -> the positions of the paragraphs it appears in.
    known_sentences: Dict[str, FrozenSet[int]] = field(default_factory=dict)


def _clause_digests(paragraphs: Sequence[TextInformation], structure: ClauseStructure) -> Dict[int, str]:
    """Digest of each paragraph's whole clause: its paragraphs' text and roles, in order."""

    members: Dict[int, List[int]] = {}
    for position, unit in sorted(structure.unit_of.items()):
        members.setdefault(unit, []).append(position)
    digests: Dict[int, str] = {}
    for positions in members.values():
        payload = "|".join(f"{structure.role(position)}:{ZDRClauseHashCache.normalize_text(paragraphs[position].text)}" for position in positions)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        for position in positions:
            digests[position] = digest
    return digests


def build_review_plan(request: GeneralReviewRequest, batch_count: int, cache: ZDRClauseHashCache) -> ReviewPlan:
    """Resolve the parties, scan the document facts, and assign paragraph roles for one review."""

    paragraphs = request.textinformation
    titles = build_clause_titles(paragraphs)
    structure = build_clause_structure(paragraphs, titles)
    facts = build_document_facts(paragraphs, titles, structure)
    parties = resolve_party_context(request.party_represented, paragraphs)
    reviewer_context = _build_reviewer_context(request, parties)
    clause_digests = _clause_digests(paragraphs, structure)
    party_key = parties if parties is not None else request.party_represented
    cache_keys = {
        idx: cache.compute_key(para.text, titles.get(para.paraindetifier, ""), party_key, request.agreement_type, reviewer_context, clause_digests.get(idx, ""), facts.digest)
        for idx, para in enumerate(paragraphs)
        if para.text.strip()
    }
    # Pre-LLM normalisation: a blank field whose value the paragraph itself supplies is shown with the value restored.
    shown_text: Dict[int, str] = {}
    for idx, para in enumerate(paragraphs):
        blank = facts.blank_field_for(para.paraindetifier)
        if blank is not None:
            shown_text[idx] = restore_blank_fields(para.text, blank)
    known: Dict[str, Set[int]] = {}
    for idx, para in enumerate(paragraphs):
        for key in sentence_keys(shown_text.get(idx, para.text)):
            known.setdefault(key, set()).add(idx)
    return ReviewPlan(
        shown_text=shown_text,
        known_sentences={key: frozenset(positions) for key, positions in known.items()},
        titles=titles,
        structure=structure,
        facts=facts,
        parties=parties,
        reviewer_context=reviewer_context,
        party_block=parties.render_block() if parties is not None else "",
        facts_block=facts.render(),
        outline_context=_build_outline_context(paragraphs, titles, batch_count),
        cache_keys=cache_keys,
    )


@dataclass
class BatchPolicies:
    """One batch's legal-team policies: the prompt block, and the policy behind each paragraph position."""

    block: str = ""
    policy_of: Dict[int, ClausePolicy] = field(default_factory=dict)


_NO_POLICIES = BatchPolicies()


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
    units = build_clause_units(paragraphs, titles)
    # A clause belongs to the batch holding its first paragraph.
    batch_of: Dict[int, int] = {idx: number for number, (batch, _) in enumerate(batches) for idx, _ in batch}
    batch_units: List[List[List[int]]] = [[] for _ in batches]
    for unit in units:
        number = batch_of.get(unit[0])
        if number is not None:
            batch_units[number].append(unit)

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
                policy = policy_for_clause(resolved.canonical_type, request.agreement_type, clause.heading)
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
    """Keep only the reported triggers that exist in the clause's policy, returned in the policy's own wording.

    A trigger matches when the model copied it verbatim, or when it ends with
    the criterion id of one of this policy's triggers — the model often copies
    only the part after the criterion's name ("There is a conflict (LAW-03)"
    for "No conflicting provisions: There is a conflict (LAW-03)"). Anything
    else (a paraphrase, an id belonging to another clause type, or a trigger
    for a clause with no policy) carries no weight in the risk computation,
    so the same wording cannot score differently run to run.
    """

    if not reported:
        return []
    if policy is None:
        logger.warning("Dropping %d trigger flag(s) for clause '%s' — the clause has no legal-team policy.", len(reported), clause)
        return []
    known = {_fold_trigger(trigger): trigger for trigger in policy.unacceptable_triggers}
    by_id = {criterion_id: trigger for trigger in policy.unacceptable_triggers for criterion_id in (criterion_id_of(trigger),) if criterion_id}
    kept: List[str] = []
    for flag in reported:
        trigger = known.get(_fold_trigger(flag))
        if trigger is None:
            criterion_id = criterion_id_of(flag)
            trigger = by_id.get(criterion_id) if criterion_id else None
        if trigger is None:
            logger.warning("Dropping trigger flag %r for clause '%s' — not a trigger of its %s policy.", flag, clause, policy.canonical_type)
        elif trigger not in kept:
            kept.append(trigger)
    return kept


def policy_for_clause(canonical_type: Optional[str], agreement_type: Optional[str], heading: str = "") -> Optional[ClausePolicy]:
    """The legal team's policy for a clause type, or None when the type is unknown.

    Policies are generated from the criteria taxonomy, so a type's gating
    criteria (AMD-02 for an amendments clause) are already among its triggers.
    """

    if not canonical_type:
        return None
    return get_clause_policy_store().get(canonical_type, None, agreement_type)


def _risk_level(finding: ReviewFinding, policy: Optional[ClausePolicy], clause_favors_client: bool = False, flags: Optional[Sequence[str]] = None, adds_obligation: bool = False) -> RiskLevel:
    """Compute the finding's risk level in code from its structured facts, the clause type's inherent tier, and which party the clause binds.

    flags are the reported triggers already verified against the clause's
    policy; they are verified here when not supplied. A verified trigger that
    is a taxonomy gate sets a floor under the computed level.

    A finding on a Medium- or High-tier clause that supplies something the
    clause lacks — a clause-wide omission, or a fix that adds a sentence —
    falls short of the standard by definition, and is never mere drafting
    hygiene. Those two facts are set in code, so the model calling the same
    addition a clarity point in one run cannot drop it to Low.
    """

    verified = list(flags) if flags is not None else _matching_triggers(finding.triggered_unacceptable_flags, policy, finding.clause)
    tier = policy.inherent_risk_tier if policy is not None else tier_for_unmapped(finding.severity_category)
    falls_short = finding.falls_short_of_acceptable_standard
    severity: str = finding.severity_category
    if policy is not None and tier in ("High", "Medium") and (finding.finding_scope == CLAUSE_WIDE_OMISSION or adds_obligation):
        falls_short = True
        severity = MATERIAL_DEVIATION if severity == MINOR_HYGIENE else severity
    level = compute_deterministic_risk_level(tier, verified, falls_short, finding.is_materially_one_sided_against_client, severity, clause_favors_client=clause_favors_client)
    return apply_gate_floor(level, strongest_gate(verified))


@dataclass(frozen=True)
class _Candidate:
    """A grounded finding on its way to becoming a suggestion."""

    position: int  # paragraph the suggestion is anchored on
    order: int  # the model's own output order
    finding: ReviewFinding
    original: str
    fix: str
    flags: Tuple[str, ...] = ()  # reported triggers that exist, verbatim, in the clause's policy or taxonomy gates
    relocated: bool = False  # moved onto its clause's primary anchor by anchor locking
    adds_text: bool = False  # the fix adds a sentence the agreement does not already contain


@dataclass(frozen=True)
class _BatchView:
    """One batch as the rules see it: each paragraph as sent, as shown to the model, and its policy."""

    raw: Dict[int, TextInformation]
    shown: Dict[int, TextInformation]
    policies: BatchPolicies
    plan: ReviewPlan


# Reasons sharing this much of their wording, in one clause, describe the same issue.
_DUPLICATE_REASON_OVERLAP = 0.6
_REASON_WORD_RE = re.compile(r"[a-z]{4,}")


def _clause_favors_client(position: int, view: _BatchView) -> bool:
    """True when the paragraph, or its clause's primary operative paragraph, binds only the counterparty."""

    parties = view.plan.parties
    anchor = view.plan.structure.anchor_of.get(position)
    for candidate in (position, anchor):
        if candidate is not None and candidate in view.raw and clause_direction(view.raw[candidate].text, parties) == FAVORS_REPRESENTED:
            return True
    return False


def _relocate_to_anchor(candidate: _Candidate, view: _BatchView) -> Optional[_Candidate]:
    """Move a clause-wide finding from the paragraph the model chose onto its clause's primary anchor.

    The sentences the model's fix added are carried over and appended to the
    anchor paragraph. A fix that only edits its own paragraph's wording has
    nothing to carry, and a heading has nothing to keep, so those are dropped;
    a body paragraph whose anchor sits outside this batch stays where it is.
    """

    structure = view.plan.structure
    position = candidate.position
    is_heading = structure.role(position) == ROLE_HEADING
    anchor = structure.anchor_of.get(position)
    if anchor is None or anchor not in view.raw:
        return None if is_heading else candidate
    additions = new_sentences(view.shown[position].text, candidate.fix)
    if not additions:
        logger.warning("Dropping clause-wide finding for clause '%s' — it was raised off the clause's primary paragraph and adds nothing that can be carried there.", candidate.finding.clause)
        return None
    logger.info("Anchor lock: clause-wide finding for clause '%s' moved to the clause's primary operative paragraph.", candidate.finding.clause)
    return replace(candidate, position=anchor, relocated=True, original=view.raw[anchor].text, fix=view.shown[anchor].text.rstrip() + " " + " ".join(additions))


def _silence_reason(candidate: _Candidate, view: _BatchView) -> Optional[str]:
    """Why a finding must not be emitted where it was raised, or None when it may be.

    Non-clause content is never flagged; a finding that contradicts a
    document fact, or reports a blank the paragraph itself fills, is wrong; a
    routine clause is flagged only on a trigger or gate; and a clause that
    runs in the client's favor is not flagged for being one-sided.
    """

    plan = view.plan
    position = candidate.position
    para = view.raw[position]
    role = plan.structure.role(position)
    reason = candidate.finding.reason
    if role == ROLE_CONTEXT_ONLY:
        return f"the paragraph is {plan.structure.zone_of.get(position, 'context only')} and is never flagged"
    contradicted = plan.facts.contradicted_claim(para.paraindetifier, reason)
    if contradicted is not None:
        return f"it reports a {contradicted} provision as missing, and the document has one elsewhere"
    if plan.facts.blank_field_for(para.paraindetifier) is not None and claims_blank_field(reason):
        return "it reports a blank field whose value the paragraph itself supplies"
    if role == ROLE_BOILERPLATE and not candidate.flags:
        return "it is a routine clause and no unacceptable trigger or taxonomy gate is set off"
    if _clause_favors_client(position, view) and is_sole_mutuality_complaint(reason) and not drop_direction_contradicted_flags(candidate.flags):
        return "its only complaint is that the clause is one-sided, and the clause runs in the client's favor"
    if _is_timing_nitpick_in_clients_favor(candidate, view):
        return "it only tightens a timing word ('promptly') in an obligation that binds the counterparty alone, in the client's favor"
    return None


# Imprecise timing in an obligation: "promptly", "immediately", "as soon as practicable", "without undue delay".
_VAGUE_TIMING_RE = re.compile(
    r"\b(promptly|immediately|forthwith|as\s+soon\s+as\s+(reasonably\s+)?(practicable|possible)|without\s+(undue\s+)?delay|reasonable\s+time|timeframe|time\s+frame|deadline)\b", re.IGNORECASE
)


def _is_timing_nitpick_in_clients_favor(candidate: _Candidate, view: _BatchView) -> bool:
    """A drafting point that only tightens a timing word in a paragraph binding the counterparty alone.

    The paragraph itself must run in the client's favor, the finding must
    set off no trigger and be a drafting point by the model's own account
    (no shortfall, or drafting hygiene), its reason must be about timing,
    and its fix must edit wording in place rather than add a sentence.
    """

    finding = candidate.finding
    para = view.raw[candidate.position]
    if candidate.flags or clause_direction(para.text, view.plan.parties) != FAVORS_REPRESENTED:
        return False
    if finding.falls_short_of_acceptable_standard and finding.severity_category != MINOR_HYGIENE:
        return False
    return bool(_VAGUE_TIMING_RE.search(finding.reason)) and not new_sentences(view.shown[candidate.position].text, candidate.fix)


def _without_known_sentences(candidate: _Candidate, view: _BatchView) -> Optional[_Candidate]:
    """Take out of a fix any sentence it adds that the agreement already contains in another paragraph.

    A fix never re-adds a provision the agreement already has — "the terms of
    this Agreement shall prevail" proposed for the Consulting Services
    clause when its own last paragraph says exactly that. When nothing of the
    fix is left, the finding is dropped. Records whether the fix still adds
    a sentence of its own.
    """

    position = candidate.position
    shown = view.shown[position].text
    additions = new_sentences(shown, candidate.fix)
    elsewhere = [sentence for sentence in additions if view.plan.known_sentences.get(fold_sentence(sentence), frozenset()) - {position}]
    if not elsewhere:
        return replace(candidate, adds_text=bool(additions))
    fix = candidate.fix
    for sentence in elsewhere:
        fix = remove_sentence(fix, sentence)
    if sentence_keys(fix) == sentence_keys(shown):
        logger.info("Dropping finding for clause '%s' — its fix only adds text the agreement already contains in another paragraph.", candidate.finding.clause)
        return None
    logger.info("Removed %d sentence(s) from the fix for clause '%s' — the agreement already contains them in another paragraph.", len(elsewhere), candidate.finding.clause)
    return replace(candidate, fix=fix, adds_text=len(additions) > len(elsewhere))


def _apply_review_rules(candidate: _Candidate, view: _BatchView) -> Optional[_Candidate]:
    """Apply the deterministic rules to one grounded finding; None when the finding must not be emitted.

    In order: the finding is checked against the rules that silence it where
    it was raised; a clause-wide finding is locked to its clause's primary
    anchor; an expense paragraph that already meets the standard is left
    alone; and a fix on a paragraph with a blank field keeps the value the
    paragraph supplies.
    """

    plan = view.plan
    structure = plan.structure
    finding = candidate.finding
    silenced = _silence_reason(candidate, view)
    if silenced is not None:
        logger.info("Dropping finding for clause '%s' — %s.", finding.clause, silenced)
        return None

    role = structure.role(candidate.position)
    policy = view.policies.policy_of.get(candidate.position)
    clause_type = structure.clause_type_of.get(candidate.position) or (policy.canonical_type if policy is not None else None)
    clause_wide = finding.finding_scope == CLAUSE_WIDE_OMISSION or is_clause_wide_topic(finding.reason, clause_type)
    if role == ROLE_HEADING or (role in (ROLE_SECONDARY, ROLE_DEFINITION) and clause_wide):
        relocated = _relocate_to_anchor(candidate, view)
        if relocated is None:
            return None
        candidate = relocated

    para_id = view.raw[candidate.position].paraindetifier
    if plan.facts.expense_terms_met(para_id) and not [flag for flag in candidate.flags if not is_expense_approval_trigger(flag)]:
        logger.info("Dropping finding for clause '%s' — the expense paragraph already requires prior written approval of reasonable, documented expenses.", finding.clause)
        return None
    without_known = _without_known_sentences(candidate, view)
    if without_known is None:
        return None
    candidate = without_known
    blank = plan.facts.blank_field_for(para_id)
    if blank is not None:
        candidate = replace(candidate, fix=apply_blank_field_guard(candidate.fix, candidate.original, blank))
    return candidate


def _drop_minor_secondary(valid: List[Tuple[int, Suggestion]], structure: ClauseStructure) -> List[Tuple[int, Suggestion]]:
    """Drop a Low suggestion on a clause's secondary paragraph when the clause's primary anchor is already flagged High.

    The clause is already in front of the reviewer at its highest level; a
    minor point on one of its other paragraphs is the kind of finding that
    comes and goes between runs.
    """

    level_at = {position: suggestion.risk_level for position, suggestion in valid}
    kept: List[Tuple[int, Suggestion]] = []
    for position, suggestion in valid:
        anchor = structure.anchor_of.get(position)
        minor = structure.role(position) in (ROLE_SECONDARY, ROLE_DEFINITION) and suggestion.risk_level == "Low"
        if minor and anchor is not None and level_at.get(anchor) == "High":
            logger.info("Dropping Low suggestion for clause '%s' — the clause's primary paragraph is already flagged High.", suggestion.clause)
            continue
        kept.append((position, suggestion))
    return kept


def _reason_words(reason: str) -> Set[str]:
    return set(_REASON_WORD_RE.findall(reason.casefold()))


def _dedupe_within_clauses(valid: List[Tuple[int, Suggestion]], structure: ClauseStructure) -> List[Tuple[int, Suggestion]]:
    """Collapse suggestions in one clause that describe the same issue, keeping the primary anchor's (else the earliest).

    Two suggestions overlap when their reasons share most of their wording.
    The result depends only on the suggestions themselves, never on the order
    the model produced them in.
    """

    ranked = sorted(valid, key=lambda pair: (structure.role(pair[0]) != ROLE_PRIMARY, pair[0]))
    kept: List[Tuple[int, Suggestion]] = []
    for position, suggestion in ranked:
        words = _reason_words(suggestion.reason)
        duplicate = False
        for other_position, other in kept:
            if structure.unit_of.get(other_position) is None or structure.unit_of.get(other_position) != structure.unit_of.get(position):
                continue
            other_words = _reason_words(other.reason)
            union = words | other_words
            if union and len(words & other_words) / len(union) >= _DUPLICATE_REASON_OVERLAP:
                duplicate = True
                break
        if duplicate:
            logger.info("Dropping overlapping suggestion for clause '%s' — the same issue is already raised in this clause.", suggestion.clause)
        else:
            kept.append((position, suggestion))
    kept.sort(key=lambda pair: pair[0])
    return kept


def _validate_batch(
    findings: List[ReviewFinding], batch: _Batch, seen_positions: Set[int], titles: Dict[str, str], policies: BatchPolicies, plan: Optional[ReviewPlan] = None
) -> List[Tuple[int, Suggestion]]:
    """Ground each finding to one paragraph, apply the deterministic review rules, compute its risk level, and build the suggestion.

    With a plan, the paragraph a suggestion ends on, and whether it is
    emitted at all, is settled here in code: role tags, document facts,
    anchor locking, and the one-suggestion-per-paragraph and per-issue filters.
    """

    paragraph_at: Dict[int, TextInformation] = dict(batch)
    # The model was shown some paragraphs normalised (a blank field restored); it copies what it was shown.
    shown_text = plan.shown_text if plan is not None else {}
    shown_batch: _Batch = [(position, TextInformation(text=shown_text[position], paraindetifier=para.paraindetifier) if position in shown_text else para) for position, para in batch]
    view = _BatchView(raw=paragraph_at, shown=dict(shown_batch), policies=policies, plan=plan) if plan is not None else None

    candidates: List[_Candidate] = []
    for order, finding in enumerate(findings):
        needle = strip_role_tags(finding.original_text)
        grounded = _ground(needle, shown_batch) or _ground(needle, batch)
        if grounded is None:
            logger.warning("Dropping finding for clause '%s' — original_text could not be grounded in a single source paragraph.", finding.clause)
            continue
        position, grounded_para, exact_text = grounded
        _, full_fix = _expand_to_paragraph(strip_role_tags(finding.suggested_fix), grounded_para, exact_text)
        flags = tuple(_matching_triggers(finding.triggered_unacceptable_flags, policies.policy_of.get(position), finding.clause))
        # original_text is always the paragraph exactly as the frontend sent it.
        candidate: Optional[_Candidate] = _Candidate(position, order, finding, paragraph_at[position].text, full_fix, flags)
        if view is not None and candidate is not None:
            candidate = _apply_review_rules(candidate, view)
        if candidate is not None:
            candidates.append(candidate)

    # One suggestion per paragraph: a paragraph's own finding wins over one moved onto it, then the model's order.
    candidates.sort(key=lambda item: (item.position, item.relocated, item.order))
    valid: List[Tuple[int, Suggestion]] = []
    for item in candidates:
        if item.position in seen_positions:
            continue
        seen_positions.add(item.position)
        para = paragraph_at[item.position]
        # The document's own clause title keeps names identical across whole-doc
        # and selection runs; the model's label only fills in when the payload
        # carries no heading for this paragraph.
        clause = titles.get(para.paraindetifier, item.finding.clause)
        favors_client = plan is not None and clause_direction(para.text, plan.parties) == FAVORS_REPRESENTED
        risk_level = _risk_level(item.finding, policies.policy_of.get(item.position), favors_client, item.flags, item.adds_text)
        suggestion = Suggestion(clause=clause, reason=item.finding.reason, risk_level=risk_level, original_text=item.original, suggested_fix=item.fix, para_identifier=para.paraindetifier)
        valid.append((item.position, suggestion))

    if plan is None:
        return valid
    return _drop_minor_secondary(_dedupe_within_clauses(valid, plan.structure), plan.structure)


async def _review_batch(batch: _Batch, context: str, plan: ReviewPlan, policy_context: str, semaphore: asyncio.Semaphore, session_id: str) -> GeneralReviewFindings:
    """Run the schema-enforced review call for one batch, with a single retry."""

    llm_model = get_bedrock_model()
    document = _format_document(batch, plan.structure, plan.shown_text)
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
                    context={
                        "document": document,
                        "reviewer_context": plan.reviewer_context,
                        "party_context": plan.party_block,
                        "document_facts": plan.facts_block,
                        "outline_context": plan.outline_context,
                        "policy_context": policy_context,
                    },
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


def _replay(para: TextInformation, cached: ClauseAssessment) -> Suggestion:
    return Suggestion(
        clause=cached.clause, reason=cached.reason, risk_level=cast(RiskLevel, cached.risk_level), original_text=para.text, suggested_fix=cached.suggested_fix, para_identifier=para.paraindetifier
    )


def _required_term_fixes(batch: _Batch, plan: ReviewPlan, picked: Dict[int, Suggestion]) -> Dict[int, Suggestion]:
    """Low suggestions for undefined terms the model left unflagged, where the agreement names the defined term to use.

    The prompt asks the model to flag every undefined term the document
    facts list; this guarantees it for the ones with a known replacement
    ("Accordor Proprietary Information" -> "Confidential Information"), so the
    suggestion appears on every run. A paragraph the model already flagged
    keeps the model's own finding.
    """

    added: Dict[int, Suggestion] = {}
    positions = {para.paraindetifier: position for position, para in batch}
    for item in plan.facts.undefined_terms:
        position = positions.get(item.para_identifier)
        if item.defined_term is None or position is None or position in picked or position in added:
            continue
        para = dict(batch)[position]
        shown = plan.shown_text.get(position, para.text)
        reason = (
            f'The paragraph uses "{item.term}", a capitalised term this agreement never defines; the defined term for this concept is "{item.defined_term}". '
            "An undefined term leaves open what the obligation covers and invites a dispute over its scope."
        )
        added[position] = Suggestion(
            clause=plan.titles.get(para.paraindetifier, item.clause),
            reason=reason,
            risk_level="Low",
            original_text=para.text,
            suggested_fix=shown.replace(item.term, item.defined_term),
            para_identifier=para.paraindetifier,
        )
        logger.info("Undefined term '%s' in clause '%s' was not raised by the model; added the replacement with the defined term.", item.term, item.clause)
    return added


def _settled_without_model(batch: _Batch, plan: ReviewPlan, cache: ZDRClauseHashCache, cached: Dict[int, Optional[ClauseAssessment]]) -> Optional[List[Tuple[int, Suggestion]]]:
    """The batch's outcome when no model call is needed, else None.

    Either every paragraph was reviewed before and is replayed from the
    clause cache, or the batch holds nothing reviewable at all.
    """

    if all(assessment is not None for assessment in cached.values()):
        logger.info("Batch of %d paragraph(s) replayed from the clause cache.", len(batch))
        return [(position, _replay(para, assessment)) for position, para in batch for assessment in (cached[position],) if assessment is not None and assessment.flagged]
    if not any(plan.structure.reviewable(position) for position, _ in batch):
        logger.info("Batch of %d paragraph(s) holds nothing reviewable (headings, party details, signature block); no model call made.", len(batch))
        for position, _ in batch:
            cache.set(plan.cache_keys[position], NO_FINDING)
        return []
    return None


async def _review_batch_consensus(
    batch: _Batch,
    context: str,
    plan: ReviewPlan,
    policy_context: "asyncio.Future[BatchPolicies]",
    semaphore: asyncio.Semaphore,
    session_id: str,
    cache: ZDRClauseHashCache,
) -> List[Tuple[int, Suggestion]]:
    """Review one batch, replaying cached outcomes for unchanged paragraphs and keeping only majority-backed findings.

    A paragraph reviewed before (same text, clause, resolved parties,
    reviewer context, and document facts, under the same policies and
    prompts) keeps its earlier outcome, so an unchanged clause never changes
    its suggestion or risk level between runs. When every paragraph of the
    batch is cached, or none of them is reviewable, no model call is made.
    """

    titles = plan.titles
    cache_keys = plan.cache_keys
    cached: Dict[int, Optional[ClauseAssessment]] = {position: cache.get(cache_keys[position]) for position, _ in batch}
    settled = _settled_without_model(batch, plan, cache, cached)
    if settled is not None:
        return settled

    # Wait for this batch's clauses to be mapped before taking a model slot.
    policies = await policy_context
    outcomes = await asyncio.gather(*[_review_batch(batch, context, plan, policies.block, semaphore, session_id) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)

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
        for position, suggestion in _validate_batch(vote.findings, batch, set(), titles, policies, plan):
            grounded.setdefault(position, suggestion)
        per_vote.append(grounded)

    counts = Counter(position for grounded in per_vote for position in grounded)
    picked: Dict[int, Suggestion] = {}
    for grounded in per_vote:
        for position, suggestion in grounded.items():
            if counts[position] >= _MAJORITY and position not in picked:
                picked[position] = suggestion
    picked.update(_required_term_fixes(batch, plan, picked))

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


def _log_plan(plan: ReviewPlan) -> None:
    """Record what was decided in code before the model was called."""

    parties = plan.parties
    if parties is not None and parties.represented is not None and parties.counterparty is not None:
        logger.info("General review parties: %r resolved by %s to %s; counterparty %s.", parties.raw, parties.method, parties.represented.describe(), parties.counterparty.describe())
    roles = Counter(plan.structure.roles.values())
    logger.info(
        "General review plan: %d reviewable paragraph(s), %d routine (trigger-gated), %d heading(s), %d context-only; survival provision %s; %d paragraph(s) shown with a blank field restored.",
        sum(roles[role] for role in (ROLE_PRIMARY, ROLE_SECONDARY, ROLE_DEFINITION)),
        roles[ROLE_BOILERPLATE],
        roles[ROLE_HEADING],
        roles[ROLE_CONTEXT_ONLY],
        "found" if plan.facts.survival is not None else "not found",
        len(plan.shown_text),
    )


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

    cache = clause_cache
    plan = build_review_plan(request, len(batches), cache)
    policy_contexts, mapping = _start_policy_mapping(request, batches, plan.titles, session_id)
    logger.info(
        "General review: %d paragraph(s) split into %d batch(es), %d votes each%s.",
        len(request.textinformation),
        len(batches),
        CONSENSUS_VOTES,
        ", with reviewer context" if plan.reviewer_context else "",
    )
    _log_plan(plan)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    try:
        results = await asyncio.gather(
            *[_review_batch_consensus(batch, context, plan, policy_context, semaphore, session_id, cache) for (batch, context), policy_context in zip(batches, policy_contexts)]
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
        cache = clause_cache
        plan = build_review_plan(request, len(batches), cache)
        policy_contexts, mapping = _start_policy_mapping(request, batches, plan.titles, session_id)
        logger.info(
            "General review stream: %d paragraph(s) split into %d batch(es), %d votes each%s.",
            len(request.textinformation),
            len(batches),
            CONSENSUS_VOTES,
            ", with reviewer context" if plan.reviewer_context else "",
        )
        _log_plan(plan)

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [asyncio.create_task(_review_batch_consensus(batch, context, plan, policy_context, semaphore, session_id, cache)) for (batch, context), policy_context in zip(batches, policy_contexts)]

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
