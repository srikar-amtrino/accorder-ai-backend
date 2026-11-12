"""General review of a contract, or of the clauses a user selected.

The review runs in four stages, and the model is asked only what code cannot decide:

1. Plan (code). The paragraphs are grouped into clauses, the parties are
   resolved from the preamble, non-clause content is set aside, and each clause
   is mapped to its clause type and that type's checklist of criteria.
2. Assessment (model, several independent answer sets). For each clause the
   model answers its checklist: every criterion is Met, Partly met, Not met or
   Not applicable, with the words it rests on. A closed question has a stable
   answer in a way that "find whatever is wrong" does not.
3. Findings (code). A criterion fails only when every answer set reports a
   shortfall on it. Which paragraph carries the finding, whether a one-sidedness point
   survives on a clause that favors the client, and the risk level all follow
   from the answers and the taxonomy in plain code.
4. Drafting (model, one call per flagged paragraph). The model is given the
   paragraph, its clause, and the points already settled, and redrafts the
   paragraph as counsel would mark it up. Its text is returned as written.
"""

import asyncio
import hashlib
import json
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, AsyncIterator, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Type, TypeAlias, TypeVar, cast

from pydantic import BaseModel

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_clause_search
from src.schemas.clause_mapping import ClauseMatch, ClausePolicy
from src.schemas.general_review import BatchAssessment, DraftedRevision, DraftingDefect, GeneralReviewRequest, GeneralReviewResponse, Suggestion, TextInformation
from src.services.clause_mapping.policy_store import get_clause_policy_store
from src.services.clause_mapping.resilient_classifier import (
    HEADING_MATCH_CONFIDENCE,
    LAYER1_HEADING,
    LAYER2_QDRANT,
    LAYER3_FALLBACK,
    MIN_CLAUSE_WORDS,
    MISCELLANEOUS_TYPE,
    SKIPPED_SHORT,
    UNCLASSIFIED,
    ResolvedClause,
    classify_clause_resilient,
    classify_layer1_heading,
)
from src.services.clause_mapping.search import BaseClauseSearch
from src.services.general_review.assessment import (
    GENERIC_CRITERIA,
    GENERIC_TIER,
    GENERIC_TYPE_LABEL,
    ClauseView,
    Point,
    answers_by_clause,
    render_clause,
    render_points,
    select_points,
    tally_criteria,
    tally_defects,
    type_fits_by_clause,
    undefined_term_points,
)
from src.services.general_review.clause_cache import NO_FINDING, ClauseAssessment, ZDRClauseHashCache, cache_ttl_seconds, clause_cache
from src.services.general_review.defined_terms import quoted_defined_terms
from src.services.general_review.document_facts import DocumentFacts, apply_blank_field_guard, build_document_facts, restore_blank_fields
from src.services.general_review.party_resolver import FAVORS_REPRESENTED, ROLE_CLIENT, PartyContext, clause_direction, resolve_party_context
from src.services.general_review.risk_matrix import RiskLevel, highest
from src.services.general_review.segmentation import (
    ROLE_CONTEXT_ONLY,
    ROLE_HEADING,
    ClauseStructure,
    build_clause_structure,
    build_clause_titles,
    build_clause_units,
    build_smart_embedding_query,
    clause_number,
    runs_on_into,
)
from src.services.general_review.taxonomy_gates import criteria_for_type, provider_protective_criteria, relocation_criteria
from src.services.general_review.typography import restore_typography, uses_curly_apostrophes, uses_curly_quotes

logger = get_logger(__name__)

# Caps parallel Bedrock calls so large documents don't exhaust the client pool.
MAX_CONCURRENT_CALLS = 6

# Independent answer sets per assessment batch. A criterion fails only when every set that came back reports a
# shortfall on it: a point the model answers both ways is left out on every run instead of coming and going.
# At least a majority of the sets must come back for the batch to be decided at all.
# The answers are short and enumerated, so three sets cost little and settle the borderline items.
CONSENSUS_VOTES = 3
_MAJORITY = CONSENSUS_VOTES // 2 + 1

# Character budget per assessment batch. A batch always holds whole clauses, so it may run over.
BATCH_CHAR_BUDGET = 6000

# The whole agreement is given to every assessment call as read-only reference up to this size, so a
# criterion is judged by what the agreement provides anywhere. A longer agreement is given as its outline.
REFERENCE_FULL_TEXT_LIMIT = 150_000

# A reference shorter than this is below the model's minimum cacheable prompt; no cache warm-up is made for it.
_MIN_CACHEABLE_REFERENCE_CHARS = 8000

ASSESS_MAX_TOKENS = 8000
DRAFT_MAX_TOKENS = 4000

_OUTLINE_MAX_ENTRIES = 120

# A document with at least this many numbered paragraphs is taken to show its clause numbers.
_NUMBERED_DOCUMENT_MIN_PARAGRAPHS = 3

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3" / "general_review"
_ASSESS_SYSTEM = (_PROMPTS_DIR / "assess_system.mustache").read_text(encoding="utf-8")
_ASSESS_USER = (_PROMPTS_DIR / "assess_user.mustache").read_text(encoding="utf-8")
_DRAFT_SYSTEM = (_PROMPTS_DIR / "draft_system.mustache").read_text(encoding="utf-8")
_DRAFT_USER = (_PROMPTS_DIR / "draft_user.mustache").read_text(encoding="utf-8")

# Identical requests return the identical review (LRU, per process). A stored review is contract text, so it is
# discarded once it is older than the configured time to live, like the clause cache's entries.
_CACHE_MAX_ENTRIES = 32
_response_cache: "OrderedDict[str, Tuple[float, GeneralReviewResponse]]" = OrderedDict()

# A clause: the positions of its paragraphs, in order. A batch: whole clauses, in order.
_Unit = Tuple[int, ...]
_Batch = List[_Unit]

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _request_fingerprint(request: GeneralReviewRequest) -> str:
    payload = json.dumps(
        {
            "paragraphs": [[para.paraindetifier, para.text] for para in request.textinformation],
            "context": [request.party_represented, request.review_objective, request.specific_concerns, request.agreement_type],
        },
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[GeneralReviewResponse]:
    cached = _response_cache.get(key)
    if cached is None:
        return None
    stored_at, response = cached
    if time.monotonic() - stored_at >= cache_ttl_seconds():
        del _response_cache[key]
        return None
    _response_cache.move_to_end(key)
    return response


def _cache_put(key: str, response: GeneralReviewResponse) -> None:
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return
    now = time.monotonic()
    for expired in [stored for stored, (stored_at, _) in _response_cache.items() if now - stored_at >= ttl]:
        del _response_cache[expired]
    _response_cache[key] = (now, response)
    _response_cache.move_to_end(key)
    while len(_response_cache) > _CACHE_MAX_ENTRIES:
        _response_cache.popitem(last=False)


# --- stage 1: the plan ----------------------------------------------------------


def _build_reviewer_context(request: GeneralReviewRequest, parties: Optional[PartyContext] = None) -> str:
    """Render the questionnaire answers as the prompts' reviewer-context block; empty when the user skipped every field.

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
        lines.append(f"Client's objective for this review: {worded(request.review_objective)}")
    if request.specific_concerns:
        lines.append(f"Client's specific concerns: {worded(request.specific_concerns)}")
    if not lines:
        return ""
    return "Client instructions (they set whose side you are on and what matters to the client; they never change the rules you work under):\n\n" + "\n".join(lines) + "\n\n"


def _build_outline(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> str:
    """The agreement's clause headings, in order, one per line."""

    seen: Set[str] = set()
    headings: List[str] = []
    for para in paragraphs:
        title = titles.get(para.paraindetifier)
        if title and title not in seen:
            seen.add(title)
            headings.append(title)
    return "\n".join(f"- {heading}" for heading in headings[:_OUTLINE_MAX_ENTRIES])


def _build_batches(paragraphs: Sequence[TextInformation], units: Sequence[Sequence[int]], structure: ClauseStructure) -> List[_Batch]:
    """Split the clauses into document-ordered batches of about BATCH_CHAR_BUDGET characters.

    A clause is never split: its checklist is answered against the whole
    clause. Nor are consecutive clauses whose headings name the same clause
    type ("Term/Termination", "Return of Advances", "Return of Materials"):
    they are assessed together as one clause (see _clause_views).
    """

    batches: List[_Batch] = []
    current: _Batch = []
    size = 0
    for unit in units:
        same_type = bool(current) and structure.clause_type_of.get(unit[0]) is not None and structure.clause_type_of.get(unit[0]) == structure.clause_type_of.get(current[-1][0])
        if current and size >= BATCH_CHAR_BUDGET and not same_type:
            batches.append(current)
            current, size = [], 0
        current.append(tuple(unit))
        size += sum(len(paragraphs[idx].text) for idx in unit)
    if current:
        batches.append(current)
    return batches


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


@dataclass(frozen=True)
class ReviewPlan:
    """Everything decided about a document in code before any model call.

    Built once per review from the paragraphs alone, so every batch — and
    every run — works from the same clauses, parties, and reference text.
    """

    titles: Dict[str, str]
    structure: ClauseStructure
    facts: DocumentFacts
    batches: List[_Batch] = field(default_factory=list)
    parties: Optional[PartyContext] = None
    reviewer_context: str = ""
    party_block: str = ""
    # Read-only reference given to every assessment call: client, parties, located provisions, and the agreement's text.
    reference: str = ""
    # What the drafting call is told about the agreement as a whole: defined terms, governing law, clause headings.
    agreement_context: str = ""
    cache_keys: Dict[int, str] = field(default_factory=dict)
    # Position -> the text the model is shown, for the paragraphs normalised before the prompts are built.
    shown_text: Dict[int, str] = field(default_factory=dict)
    # Whether the paragraphs carry their clause numbers. Automatic numbering is often lost on the way from Word.
    numbered: bool = False
    # How the agreement types its apostrophes and quotation marks; new wording in a redraft is typed the same way.
    curly_apostrophes: bool = False
    curly_quotes: bool = False
    # Positions of the paragraphs whose last sentence carries on in the next paragraph (a paragraph break inside a sentence).
    runs_on: FrozenSet[int] = frozenset()


def _build_reference(paragraphs: Sequence[TextInformation], shown_text: Dict[int, str], reviewer_context: str, party_block: str, facts_block: str, outline: str, terms: Sequence[str]) -> str:
    parts = ["Reference (read-only: use it to understand the clauses under assessment; never assess or revise anything in it).\n\n", reviewer_context, party_block, facts_block]
    full_text = "\n\n".join(shown_text.get(idx, para.text).strip() for idx, para in enumerate(paragraphs) if para.text.strip())
    if len(full_text) <= REFERENCE_FULL_TEXT_LIMIT:
        parts.append(f"Full text of the agreement:\n\n<agreement>\n{full_text}\n</agreement>\n\n")
    else:
        if outline:
            parts.append(f"The agreement is long, so its clause headings are listed in place of its full text:\n\n{outline}\n\n")
        if terms:
            parts.append("Terms the agreement defines: " + ", ".join(f'"{term}"' for term in terms) + "\n\n")
    return "".join(parts)


def _build_agreement_context(facts: DocumentFacts, outline: str, terms: Sequence[str]) -> str:
    parts: List[str] = []
    if terms:
        parts.append("Defined terms of the agreement: " + ", ".join(f'"{term}"' for term in terms) + "\n\n")
    if facts.governing_state:
        parts.append(f"Governing law: {facts.governing_state}\n\n")
    if facts.attachments_not_supplied:
        parts.append(f"{facts.attachments_note()}\n\n")
    if outline:
        parts.append(f"Clause headings of the agreement, in order:\n\n{outline}\n\n")
    return "".join(parts)


def build_review_plan(request: GeneralReviewRequest, cache: ZDRClauseHashCache) -> ReviewPlan:
    """Group the paragraphs into clauses and batches, resolve the parties, scan the document facts, and build the shared prompt blocks."""

    paragraphs = request.textinformation
    titles = build_clause_titles(paragraphs)
    structure = build_clause_structure(paragraphs, titles)
    facts = build_document_facts(paragraphs, titles, structure)
    parties = resolve_party_context(request.party_represented, paragraphs)
    reviewer_context = _build_reviewer_context(request, parties)
    party_block = parties.render_block() if parties is not None else ""
    clause_digests = _clause_digests(paragraphs, structure)
    party_key = parties if parties is not None else request.party_represented
    cache_keys = {
        idx: cache.compute_key(para.text, titles.get(para.paraindetifier, ""), party_key, request.agreement_type, reviewer_context, clause_digests.get(idx, ""), facts.digest)
        for idx, para in enumerate(paragraphs)
        if para.text.strip()
    }
    # A blank field whose value the paragraph itself supplies is shown with the value restored.
    shown_text: Dict[int, str] = {}
    for idx, para in enumerate(paragraphs):
        blank = facts.blank_field_for(para.paraindetifier)
        if blank is not None:
            shown_text[idx] = restore_blank_fields(para.text, blank)
    outline = _build_outline(paragraphs, titles)
    terms = quoted_defined_terms(paragraphs)
    document_text = "\n".join(para.text for para in paragraphs)
    filled = [idx for idx, para in enumerate(paragraphs) if para.text.strip()]
    return ReviewPlan(
        runs_on=frozenset(idx for idx, following in zip(filled, filled[1:]) if runs_on_into(paragraphs[idx].text, paragraphs[following].text)),
        titles=titles,
        structure=structure,
        facts=facts,
        batches=_build_batches(paragraphs, build_clause_units(paragraphs, titles), structure),
        numbered=sum(1 for para in paragraphs if clause_number(para.text) is not None) >= _NUMBERED_DOCUMENT_MIN_PARAGRAPHS,
        curly_apostrophes=uses_curly_apostrophes(document_text),
        curly_quotes=uses_curly_quotes(document_text),
        parties=parties,
        reviewer_context=reviewer_context,
        party_block=party_block,
        reference=_build_reference(paragraphs, shown_text, reviewer_context, party_block, facts.render(), outline, terms),
        agreement_context=_build_agreement_context(facts, outline, terms),
        cache_keys=cache_keys,
        shown_text=shown_text,
    )


# --- stage 1, continued: clause types -------------------------------------------

# Total time the vector path (embedding + Qdrant) may spend per review. Past
# this budget the remaining clauses are classified offline (layers 1 and 3).
POLICY_MAPPING_BUDGET_SECONDS = 240.0


@dataclass
class _UnitPolicies:
    """One batch's clause types, keyed by the position of each clause's first paragraph."""

    # The policy (clause type and inherent risk tier) of each clause that mapped to a type.
    policies: Dict[int, ClausePolicy] = field(default_factory=dict)
    # The clauses whose own heading names their type. Any other type was inferred, from the clause's opening words
    # or from similar clauses in the vector DB, and is confirmed by the model before its checklist counts.
    named_by_heading: Set[int] = field(default_factory=set)


_PolicyContexts = List["asyncio.Future[_UnitPolicies]"]


def policy_for_clause(canonical_type: Optional[str], agreement_type: Optional[str]) -> Optional[ClausePolicy]:
    """The legal team's policy for a clause type (its inherent risk tier), or None when the type is unknown."""

    if not canonical_type:
        return None
    return get_clause_policy_store().get(canonical_type, None, agreement_type)


def _start_policy_mapping(request: GeneralReviewRequest, plan: ReviewPlan, session_id: str) -> Tuple[_PolicyContexts, "asyncio.Task[None]"]:
    """Begin mapping clauses to clause types, one batch at a time, ahead of the review.

    Returns a future per batch that resolves to that batch's clause types, and
    the background task doing the mapping. Each batch's review awaits only its
    own, so the first model call starts after the first batch's clauses are
    mapped rather than after the whole document is. The mapping never blocks
    or fails a review: when the vector path is unavailable the offline
    classifier still names every clause type it can.
    """

    loop = asyncio.get_running_loop()
    contexts: _PolicyContexts = [loop.create_future() for _ in plan.batches]
    task = asyncio.create_task(_map_policies(get_clause_search(), request, plan.batches, plan.titles, session_id, contexts))
    return contexts, task


@dataclass
class _ClauseQuery:
    """A clause prepared for classification."""

    unit: _Unit
    heading: str
    query: str
    word_count: int
    needs_vector: bool


async def _map_policies(search: Optional[BaseClauseSearch], request: GeneralReviewRequest, batches: Sequence[_Batch], titles: Dict[str, str], session_id: str, contexts: _PolicyContexts) -> None:
    """Map each batch's clauses to one clause type each, in document order.

    Layer 1 locks clauses whose heading names their type without any network
    call. The rest of a batch's clauses go to the vector DB in one request;
    if that path is disabled, fails, or exhausts its budget, every remaining
    clause is classified offline instead.
    """

    paragraphs = request.textinformation
    mapped = 0
    source_counts: Counter = Counter()
    vector_available = search is not None
    started = asyncio.get_running_loop().time()
    completed = 0
    try:
        for number, batch in enumerate(batches):
            prepared: List[_ClauseQuery] = []
            for unit in batch:
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

            policies = _UnitPolicies()
            for clause in prepared:
                resolved = classify_clause_resilient(clause.heading, clause.query, clause.word_count, hits_of.get(clause.unit[0]) if vector_available else None)
                source_counts[resolved.source] += 1
                policy = policy_for_clause(resolved.canonical_type, request.agreement_type)
                if policy is None:
                    continue
                _log_resolution(session_id, clause, resolved)
                policies.policies[clause.unit[0]] = policy
                if resolved.source == LAYER1_HEADING:
                    policies.named_by_heading.add(clause.unit[0])
            mapped += len(policies.policies)
            contexts[number].set_result(policies)
            completed = number + 1
    except Exception:
        logger.exception("Clause mapping failed after %d of %d batch(es); the remaining clauses are reviewed against the general checklist.", completed, len(batches))
    finally:
        for context in contexts:
            if not context.done():
                context.set_result(_UnitPolicies())
        logger.info(
            "Clause mapping: %d of %d clause(s) mapped to a clause type across %d of %d batch(es) in %.1fs (%s).",
            mapped,
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


def _clause_views(batch: _Batch, plan: ReviewPlan, paragraphs: Sequence[TextInformation], policies: _UnitPolicies) -> List[ClauseView]:
    """The clauses of a batch that are assessed, each with its checklist.

    A clause of a known type gets that type's criteria and inherent risk tier;
    any other clause of substance gets the general checklist. A type the
    clause's own heading does not name is marked unconfirmed: a wrong type
    would have the review ask for terms the clause has no business containing.
    A clause made only of non-clause content (preamble, signature block) or
    of a few words is not assessed.
    """

    # Consecutive clauses of one clause type are one clause for assessment: "Return of Advances" and "Return of
    # Materials" are part of Term/Termination, and the type's checklist is answered once for all of them. The
    # catch-all type is left out: two miscellaneous clauses side by side have nothing in common.
    groups: List[Tuple[List[int], Optional[ClausePolicy], List[bool]]] = []
    for unit in batch:
        policy = policies.policies.get(unit[0])
        named = unit[0] in policies.named_by_heading
        previous = groups[-1][1] if groups else None
        if policy is not None and previous is not None and policy.canonical_type == previous.canonical_type and policy.canonical_type != MISCELLANEOUS_TYPE:
            groups[-1][0].extend(unit)
            groups[-1][2].append(named)
        else:
            groups.append((list(unit), policy, [named]))

    views: List[ClauseView] = []
    for members, policy, named_flags in groups:
        assessable = frozenset(idx for idx in members if plan.structure.reviewable(idx))
        if not assessable:
            continue
        criteria = criteria_for_type(policy.canonical_type) if policy is not None else ()
        tier: str
        fallback = ""
        if policy is not None and criteria:
            tier, label, fallback = policy.inherent_risk_tier, criteria[0].clause_type, policy.fallback_guidelines
        elif sum(len(paragraphs[idx].text.split()) for idx in members) >= MIN_CLAUSE_WORDS:
            criteria, tier, label = GENERIC_CRITERIA, GENERIC_TIER, GENERIC_TYPE_LABEL
        else:
            continue
        anchor = plan.structure.anchor_of.get(min(assessable))
        view = ClauseView(
            clause_id=f"C{len(views) + 1}",
            title=plan.titles.get(paragraphs[members[0]].paraindetifier, ""),
            type_label=label,
            positions=tuple(members),
            assessable=assessable,
            anchor=anchor if anchor in assessable else min(assessable),
            criteria=criteria,
            tier=tier,
            type_confirmed=any(named_flags),
            fallback=fallback,
        )
        if view.asked:
            views.append(view)
    return views


# --- stages 2 and 4: the model calls --------------------------------------------


async def _generate(response_model: Type[ResponseT], prompt: str, context: Dict[str, Any], system_message: str, session_id: str, max_tokens: int, cached_prefix: Optional[str] = None) -> ResponseT:
    """One schema-enforced model call at temperature 0, retried once on failure."""

    llm_model = get_bedrock_model()
    for attempt in (1, 2):
        try:
            return await llm_model.generate(
                prompt=prompt,
                context=context,
                response_model=response_model,
                system_message=system_message,
                session_id=session_id,
                temperature=0.0,
                max_tokens=max_tokens,
                cached_prefix=cached_prefix,
                fail_on_truncation=True,
            )
        except Exception:
            if attempt == 2:
                raise
            logger.exception("%s call failed on attempt %d; retrying once.", response_model.__name__, attempt)
            await asyncio.sleep(2)
    raise RuntimeError("unreachable")


class _ReferenceWarmup:
    """Writes the shared reference to the model's prompt cache once, before the assessment calls fan out.

    Calls that start together all miss the cache, because an entry exists only
    once a first response has begun. One request limited to a single output
    token writes the entry in a second or two; every assessment call after it
    reads the reference from the cache instead of paying for it again.
    """

    def __init__(self, reference: str, session_id: str) -> None:
        self._reference = reference
        self._session_id = session_id
        self._task: Optional["asyncio.Task[None]"] = None

    async def ensure(self) -> None:
        if len(self._reference) < _MIN_CACHEABLE_REFERENCE_CHARS:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._warm())
        await self._task

    async def _warm(self) -> None:
        try:
            await get_bedrock_model().warm_prompt_cache(self._reference, BatchAssessment, self._session_id, _ASSESS_SYSTEM)
        except Exception:
            # The warm-up only saves cost; the assessment calls run the same without it.
            logger.warning("Prompt cache warm-up failed; the assessment calls will each send the reference in full.", exc_info=True)


@dataclass
class _ReviewRun:
    """What every batch of one review shares."""

    request: GeneralReviewRequest
    plan: ReviewPlan
    cache: ZDRClauseHashCache
    semaphore: asyncio.Semaphore
    warmup: _ReferenceWarmup
    session_id: str
    trace: Optional[List[Dict[str, Any]]] = None
    # Set when an answer set or a redraft was lost: the review is returned but not stored as this document's review.
    incomplete: bool = False


async def _assess(clauses_text: str, run: _ReviewRun) -> BatchAssessment:
    """One independent set of checklist answers for a batch."""

    await run.warmup.ensure()
    async with run.semaphore:
        context = {"clauses": clauses_text, "attachments": run.plan.facts.attachments_reminder()}
        return await _generate(BatchAssessment, _ASSESS_USER, context, _ASSESS_SYSTEM, run.session_id, ASSESS_MAX_TOKENS, cached_prefix=run.plan.reference)


_COPIED_ID_RE = re.compile(r"^\s*\[P\d+\]\s*")

# Shown to the assessment after a paragraph whose last sentence carries on in the next paragraph.
_RUNS_ON_MARK = "[the sentence continues in the next paragraph]"

# A placeholder with nothing in it ("[*]", "[ ]", "____"): what belongs there is for the parties, never for a redraft.
_EMPTY_PLACEHOLDER_RE = re.compile(r"\[[\s*●•_.…-]*\]|_{3,}")

# How many words a redraft may add: a base and an allowance for each point it has to resolve, up to a ceiling. The
# ceiling is what keeps a redraft a markup: a schedule of service levels or a set of data-processing terms does not
# fit in it, so the drafter has to leave that detail to the document that should hold it.
_ADDED_WORDS_BASE = 40
_ADDED_WORDS_PER_POINT = 15
_ADDED_WORDS_CEILING = 100
# A redraft that runs past its allowance by more than this share is sent back once to be cut down.
_ADDED_WORDS_TOLERANCE = 0.25


def _added_words_allowed(points: Sequence[Point]) -> int:
    return min(_ADDED_WORDS_CEILING, _ADDED_WORDS_BASE + _ADDED_WORDS_PER_POINT * len(points))


def _words_added(original: str, revised: str) -> int:
    """How many words the redraft writes into the paragraph (insertions and replacements; deletions do not count)."""

    before, after = original.split(), revised.split()
    return sum(j2 - j1 for tag, _, _, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if tag in ("insert", "replace"))


def _paragraph_notes(position: int, run: "_ReviewRun") -> str:
    """What the drafter is told about the paragraph beyond its words: how it is laid out, and whom it binds."""

    plan = run.plan
    paragraphs = run.request.textinformation
    notes: List[str] = []
    if position in plan.runs_on:
        notes.append("The paragraph stops in the middle of a sentence; the sentence carries on in the next paragraph of the clause. Leave its last words as they are and do not complete the sentence.")
    if _follows_a_run_on(position, run):
        notes.append("The paragraph opens in the middle of a sentence begun in the paragraph above it. Leave its opening words as they are.")
    if plan.parties is not None and plan.parties.resolved and clause_direction(paragraphs[position].text, plan.parties) == FAVORS_REPRESENTED:
        counterparty = plan.parties.counterparty.alias if plan.parties.counterparty is not None else "the Counterparty"
        notes.append(
            f"As it stands the paragraph places obligations on {counterparty} alone. Keep it that way: do not extend any of them to your client, "
            'and do not rewrite them as obligations of "each Party" or "either Party", whatever a point says about both parties.'
        )
    if _EMPTY_PLACEHOLDER_RE.search(paragraphs[position].text):
        notes.append("The paragraph contains an entry left blank or redacted. Leave every such entry exactly as it is.")
    relied_on = plan.facts.attachments_relied_on(paragraphs[position].text)
    if relied_on:
        documents = " and ".join(relied_on)
        notes.append(
            f"The paragraph relies on {documents}, whose text was not provided. What that document covers is taken to be provided for there. "
            "Do not write its content into the paragraph (no tables of service levels, severity categories, response times, security measures or processing terms); "
            "where a point concerns such a matter, require it by reference to that document."
        )
    return "".join(f"- {note}\n" for note in notes)


def _follows_a_run_on(position: int, run: "_ReviewRun") -> bool:
    paragraphs = run.request.textinformation
    for earlier in range(position - 1, -1, -1):
        if paragraphs[earlier].text.strip():
            return earlier in run.plan.runs_on
    return False


def _same_text(left: str, right: str) -> bool:
    return " ".join(left.split()) == " ".join(right.split())


# A redraft must still be the paragraph it revises. One that keeps less than this share of the paragraph's words has
# put something else in its place (the definition of another term, a different clause) and is not offered.
_MIN_RETAINED_SHARE = 0.3
_MIN_WORDS_FOR_RETENTION_CHECK = 8


def _retained_share(original: str, revised: str) -> float:
    """The share of the original paragraph's words that the redraft keeps, in order."""

    before, after = original.split(), revised.split()
    if len(before) < _MIN_WORDS_FOR_RETENTION_CHECK:
        return 1.0
    kept = sum(block.size for block in SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks())
    return kept / len(before)


# A redraft that takes this many words out and puts none in has removed a provision rather than revised one.
_MIN_WORDS_FOR_A_REMOVED_PROVISION = 8


def _only_deletes(original: str, revised: str) -> bool:
    """True when the redraft is the paragraph with a provision removed and nothing written in its place.

    Striking a sentence that binds a party gives up the provision; where it
    should go instead is for counsel to decide, not for a one-paragraph redraft.
    """

    before, after = original.split(), revised.split()
    operations = [code for code in SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if code[0] != "equal"]
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in operations if tag == "delete")
    return bool(operations) and all(tag == "delete" for tag, *_ in operations) and removed >= _MIN_WORDS_FOR_A_REMOVED_PROVISION


async def _draft(position: int, points: Sequence[Point], clause: ClauseView, run: _ReviewRun) -> Optional[Suggestion]:
    """Redraft one paragraph to resolve its points; None when no usable redraft came back.

    The redraft's wording is returned as the model wrote it. Code changes
    two things only, neither of them a word: wherever the redraft repeats the
    source paragraph, the source's own characters (curly quotes, spacing) are
    put back so they do not show as edits; and a paragraph whose jurisdiction
    field arrived blank keeps the value the paragraph itself supplies.
    """

    plan = run.plan
    paragraphs = run.request.textinformation
    para = paragraphs[position]
    shown = plan.shown_text.get(position, para.text).strip()
    if len(clause.positions) > 1:
        clause_text = "\n\n".join(plan.shown_text.get(idx, paragraphs[idx].text).strip() for idx in clause.positions)
    else:
        clause_text = "(The clause consists of the paragraph below.)"
    title = plan.titles.get(para.paraindetifier, "") or clause.title
    context = {
        "reviewer_context": plan.reviewer_context,
        "party_context": plan.party_block,
        "agreement_context": plan.agreement_context,
        "clause_title": title or clause.type_label,
        "clause_text": clause_text,
        "paragraph": shown,
        "paragraph_notes": _paragraph_notes(position, run),
        "points": render_points(points),
        "fallback": clause.fallback,
        "added_words": _added_words_allowed(points),
    }
    try:
        async with run.semaphore:
            revision = await _generate(DraftedRevision, _DRAFT_USER, context, _DRAFT_SYSTEM, run.session_id, DRAFT_MAX_TOKENS)
            added = _words_added(shown, revision.revised_paragraph)
            if added > _added_words_allowed(points) * (1 + _ADDED_WORDS_TOLERANCE):
                # Too long to be a markup: asked once more, told how far over it ran. Whatever comes back is used.
                logger.info("Redraft for clause '%s' added %d words against an allowance of %d; asking once for a shorter one.", title, added, _added_words_allowed(points))
                revision = await _generate(DraftedRevision, _DRAFT_USER, {**context, "over_length": added}, _DRAFT_SYSTEM, run.session_id, DRAFT_MAX_TOKENS)
    except Exception:
        logger.exception("Drafting failed for clause '%s'; the paragraph is left without a suggestion on this run.", title)
        run.incomplete = True
        return None

    fix = _COPIED_ID_RE.sub("", revision.revised_paragraph).strip()
    blank = plan.facts.blank_field_for(para.paraindetifier)
    if blank is not None:
        fix = apply_blank_field_guard(fix, para.text, blank)
    if fix:
        fix = restore_typography(plan.shown_text.get(position, para.text), fix, plan.curly_apostrophes, plan.curly_quotes)
    if not fix or not revision.comment:
        logger.warning("Dropping suggestion for clause '%s': the redraft came back empty.", title)
        run.incomplete = True
        return None
    if _same_text(fix, shown) or _same_text(fix, para.text):
        # The drafter leaves a paragraph as it is when none of its points can be resolved in the client's favour.
        logger.info("No suggestion for clause '%s': the redraft leaves the paragraph as it stands.", title)
        return None
    if _retained_share(shown, fix) < _MIN_RETAINED_SHARE:
        logger.warning("Dropping suggestion for clause '%s': the redraft keeps too little of the paragraph to be a revision of it.", title)
        run.incomplete = True
        return None
    if len(_EMPTY_PLACEHOLDER_RE.findall(fix)) < len(_EMPTY_PLACEHOLDER_RE.findall(shown)):
        logger.warning("Dropping suggestion for clause '%s': the redraft filled in an entry the agreement leaves blank or redacted.", title)
        run.incomplete = True
        return None
    if _only_deletes(shown, fix):
        logger.info("No suggestion for clause '%s': the redraft only deletes wording from the paragraph.", title)
        return None
    risk_level: RiskLevel = highest(point.risk_level for point in points)
    return Suggestion(clause=title or clause.type_label, reason=revision.comment, risk_level=risk_level, original_text=para.text, suggested_fix=fix, para_identifier=para.paraindetifier)


# --- stage 3: findings, and one batch end to end --------------------------------


def _replay(para: TextInformation, cached: ClauseAssessment) -> Suggestion:
    return Suggestion(
        clause=cached.clause, reason=cached.reason, risk_level=cast(RiskLevel, cached.risk_level), original_text=para.text, suggested_fix=cached.suggested_fix, para_identifier=para.paraindetifier
    )


_NUMBERED_REFERENCE_RE = re.compile(r"\b(?:Sections?|Clauses?|Articles?|Paragraphs?)\s+\d", re.IGNORECASE)


def _without_unverifiable_defects(vote: BatchAssessment, plan: ReviewPlan) -> BatchAssessment:
    """An answer set without the cross-reference defects that cannot be checked.

    When the paragraphs arrived without their clause numbers, "Section 2.1"
    cannot be shown to point at the wrong clause, so a numbered reference is
    never reported as broken. Nor is a reference to an exhibit or schedule
    whose text was not among the paragraphs sent.
    """

    attachments = plan.facts.attachments_not_supplied

    def unverifiable(defect: DraftingDefect) -> bool:
        if defect.defect_type != "broken_cross_reference":
            return False
        if not plan.numbered and _NUMBERED_REFERENCE_RE.search(defect.evidence):
            return True
        # An exhibit whose text was not sent for review is attached to the agreement, not missing from it.
        return any(re.search(rf"\b{re.escape(name)}\b", defect.evidence, re.IGNORECASE) for name in attachments)

    kept = [defect for defect in vote.drafting_defects if not unverifiable(defect)]
    return vote if len(kept) == len(vote.drafting_defects) else vote.model_copy(update={"drafting_defects": kept})


def _points_for_batch(clauses: Sequence[ClauseView], votes: Sequence[BatchAssessment], run: _ReviewRun, positions: Sequence[int]) -> Dict[int, List[Point]]:
    """The points each paragraph of a batch must resolve, decided from the answer sets in code."""

    plan = run.plan
    paragraphs = run.request.textinformation

    def favors_client(position: int) -> bool:
        return clause_direction(paragraphs[position].text, plan.parties) == FAVORS_REPRESENTED

    client_is_customer = plan.parties is not None and plan.parties.represented is not None and plan.parties.represented.role == ROLE_CLIENT
    indexed = [answers_by_clause(vote) for vote in votes]
    fits = [type_fits_by_clause(vote) for vote in votes]
    points: List[Point] = []
    assessable: Set[int] = set()
    shown = {position: plan.shown_text.get(position, paragraphs[position].text) for position in positions}
    for asked in clauses:
        assessable |= asked.assessable
        # An inferred clause type counts only when every answer set says the clause really is of that type.
        clause = asked.settled(all(vote.get(asked.clause_id, False) for vote in fits))
        if clause is not asked:
            logger.info("Clause '%s' is not a %s clause on the model's reading; assessed against the general checklist.", asked.title, asked.type_label)
        clause_votes = [vote.get(clause.clause_id, {}) for vote in indexed]
        failed = tally_criteria(clause, clause_votes, _MAJORITY, favors_client, required=len(votes), text_of=shown)
        failed = [point for point in failed if point.key not in relocation_criteria()]
        if client_is_customer:
            # A term that would protect only the service provider is not something the customer's counsel asks for.
            failed = [point for point in failed if point.key not in provider_protective_criteria()]
        points.extend(failed)
        if run.trace is not None:
            run.trace.append(
                {
                    "clause": clause.title or clause.type_label,
                    "verdicts": {criterion.criterion_id: [vote[criterion.criterion_id].verdict if criterion.criterion_id in vote else None for vote in clause_votes] for criterion in clause.asked},
                }
            )
    in_batch = {paragraphs[position].paraindetifier: position for position in positions if position in assessable}
    points.extend(undefined_term_points(plan.facts.undefined_terms, in_batch))
    text_of = {position: shown[position] for position in assessable}
    points.extend(tally_defects([_without_unverifiable_defects(vote, plan) for vote in votes], text_of, frozenset(assessable), len(votes)))
    return select_points(points)


async def _review_batch(batch: _Batch, policy_context: "asyncio.Future[_UnitPolicies]", run: _ReviewRun) -> List[Tuple[int, Suggestion]]:
    """Review one batch of clauses: assess, decide the findings, draft, and remember the outcome.

    A paragraph reviewed before (same text, clause, resolved parties, client
    instructions and document facts, under the same taxonomy and prompts)
    keeps its earlier outcome. When every paragraph of the batch is cached, or
    none of its clauses is assessable, no model call is made. An outcome is
    remembered only when the batch was reviewed in full: every answer set came
    back, and — for a flagged paragraph — its redraft did too.
    """

    plan, cache = run.plan, run.cache
    paragraphs = run.request.textinformation
    positions = [idx for unit in batch for idx in unit]
    cached: Dict[int, Optional[ClauseAssessment]] = {position: cache.get(plan.cache_keys[position]) for position in positions}

    def replayed() -> Dict[int, Suggestion]:
        return {position: _replay(paragraphs[position], assessment) for position, assessment in cached.items() if assessment is not None and assessment.flagged}

    if all(assessment is not None for assessment in cached.values()):
        logger.info("Batch of %d paragraph(s) replayed from the clause cache.", len(positions))
        return sorted(replayed().items())

    # Wait for this batch's clause types before any model call.
    clauses = _clause_views(batch, plan, paragraphs, await policy_context)
    if not clauses:
        logger.info("Batch of %d paragraph(s) holds nothing to assess (headings, party details, signature block); no model call made.", len(positions))
        for position in positions:
            if cached[position] is None:
                cache.set(plan.cache_keys[position], NO_FINDING)
        return sorted(replayed().items())

    # A sentence broken across two paragraphs is marked, so the break is not read as a truncated sentence.
    text_of = {position: plan.shown_text.get(position, paragraphs[position].text).strip() + (f" {_RUNS_ON_MARK}" if position in plan.runs_on else "") for position in positions}
    headings = frozenset(position for position in positions if plan.structure.role(position) == ROLE_HEADING)
    clauses_text = "\n\n\n".join(render_clause(clause, text_of, headings) for clause in clauses)
    outcomes = await asyncio.gather(*[_assess(clauses_text, run) for _ in range(CONSENSUS_VOTES)], return_exceptions=True)
    votes = [outcome for outcome in outcomes if isinstance(outcome, BatchAssessment)]
    if len(votes) < _MAJORITY:
        raise next(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    complete = len(votes) == CONSENSUS_VOTES
    if not complete:
        run.incomplete = True
        logger.warning("Batch assessed with %d of %d answer sets; its outcome is not cached.", len(votes), CONSENSUS_VOTES)

    points = _points_for_batch(clauses, votes, run, positions)
    clause_of = {position: clause for clause in clauses for position in clause.positions}
    to_draft = [position for position in points if cached.get(position) is None and position in clause_of]
    drafted = await asyncio.gather(*[_draft(position, points[position], clause_of[position], run) for position in to_draft])
    fresh: Dict[int, Suggestion] = {position: suggestion for position, suggestion in zip(to_draft, drafted) if suggestion is not None}
    if run.trace is not None:
        run.trace.append({"points": {paragraphs[position].paraindetifier: [point.key for point in points[position]] for position in to_draft}})

    result = replayed()
    for position in positions:
        if cached[position] is not None:
            continue
        suggestion = fresh.get(position)
        if suggestion is not None:
            result[position] = suggestion
            if complete:
                cache.set(plan.cache_keys[position], ClauseAssessment(True, suggestion.clause, suggestion.risk_level, suggestion.reason, suggestion.suggested_fix))
        elif complete and position not in points:
            cache.set(plan.cache_keys[position], NO_FINDING)
    return sorted(result.items())


# --- the service ----------------------------------------------------------------


def _log_plan(plan: ReviewPlan, paragraph_count: int) -> None:
    """Record what was decided in code before the model was called."""

    parties = plan.parties
    if parties is not None and parties.represented is not None and parties.counterparty is not None:
        logger.info("General review parties: %r resolved by %s to %s; counterparty %s.", parties.raw, parties.method, parties.represented.describe(), parties.counterparty.describe())
    roles = Counter(plan.structure.roles.values())
    logger.info(
        "General review plan: %d paragraph(s) in %d clause(s) and %d batch(es), %d answer sets each; %d heading(s), %d context-only; %d paragraph(s) shown with a blank field restored.",
        paragraph_count,
        sum(len(batch) for batch in plan.batches),
        len(plan.batches),
        CONSENSUS_VOTES,
        roles[ROLE_HEADING],
        roles[ROLE_CONTEXT_ONLY],
        len(plan.shown_text),
    )


_BatchTask: TypeAlias = "asyncio.Task[List[Tuple[int, Suggestion]]]"


def _start_review(request: GeneralReviewRequest, session_id: str, trace: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[_BatchTask], Optional["asyncio.Task[None]"], _ReviewRun]:
    """Plan the review and start one task per batch; returns the batch tasks, in document order, the clause-mapping task, and the run they share."""

    cache = clause_cache
    plan = build_review_plan(request, cache)
    run = _ReviewRun(request, plan, cache, asyncio.Semaphore(MAX_CONCURRENT_CALLS), _ReferenceWarmup(plan.reference, session_id), session_id, trace)
    if not plan.batches:
        return [], None, run
    _log_plan(plan, len(request.textinformation))
    policy_contexts, mapping = _start_policy_mapping(request, plan, session_id)
    tasks = [asyncio.create_task(_review_batch(batch, policy_context, run)) for batch, policy_context in zip(plan.batches, policy_contexts)]
    return tasks, mapping, run


async def review_with_trace(request: GeneralReviewRequest, session_id: str) -> Tuple[GeneralReviewResponse, List[Dict[str, Any]]]:
    """Run the review without the response cache and return it with a trace of every checklist answer and every point raised.

    For evaluation: the trace shows which criteria each answer set failed and
    which points each flagged paragraph was redrafted to resolve.
    """

    trace: List[Dict[str, Any]] = []
    response, _ = await _run_review(request, session_id, trace)
    return response, trace


async def _run_review(request: GeneralReviewRequest, session_id: str, trace: Optional[List[Dict[str, Any]]] = None) -> Tuple[GeneralReviewResponse, bool]:
    """Run every batch and return the review, and whether it is complete (no answer set or redraft was lost)."""

    tasks, mapping, run = _start_review(request, session_id, trace)
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        raise
    finally:
        if mapping is not None:
            mapping.cancel()
    positioned = sorted((pair for batch_result in results for pair in batch_result), key=lambda pair: pair[0])
    return GeneralReviewResponse(suggestions=[suggestion for _, suggestion in positioned]), not run.incomplete


async def general_review_service(request: GeneralReviewRequest, session_id: str) -> GeneralReviewResponse:
    """Review the full document or the selected clauses the frontend sent."""

    fingerprint = _request_fingerprint(request)
    cached = _cache_get(fingerprint)
    if cached is not None:
        logger.info("General review: cache hit — returning the stored review for this document.")
        return cached
    response, complete = await _run_review(request, session_id)
    if complete:
        _cache_put(fingerprint, response)
    return response


async def general_review_streaming_service(request: GeneralReviewRequest, session_id: str) -> AsyncIterator[str]:
    """Stream the review as SSE text fragments that concatenate into one valid JSON response.

    Batches run in parallel; each batch's suggestions are emitted in document
    order as soon as that batch completes.
    """

    def frame(fragment: str) -> str:
        return f"data: {json.dumps(fragment)}\n\n"

    tasks: List[_BatchTask] = []
    mapping: Optional["asyncio.Task[None]"] = None
    try:
        fingerprint = _request_fingerprint(request)
        cached = _cache_get(fingerprint)
        if cached is not None:
            logger.info("General review stream: cache hit — streaming the stored review for this document.")
            yield frame('{"suggestions": [')
            for index, suggestion in enumerate(cached.suggestions):
                yield frame(("" if index == 0 else ",") + suggestion.model_dump_json())
            yield frame("]}")
            yield "data: [DONE]\n\n"
            return

        tasks, mapping, run = _start_review(request, session_id)
        yield frame('{"suggestions": [')
        emitted: List[Suggestion] = []
        for task in tasks:
            for _, suggestion in await task:
                yield frame(("" if not emitted else ",") + suggestion.model_dump_json())
                emitted.append(suggestion)
        yield frame("]}")
        if not run.incomplete:
            _cache_put(fingerprint, GeneralReviewResponse(suggestions=emitted))

    except Exception as exc:
        logger.exception("General review stream failed: %s", exc)
        yield f"data: {json.dumps({'error': 'General review failed. Please try again.'})}\n\n"
    finally:
        # Also reached when the client disconnects mid-stream: nothing is left running for a response no one reads.
        for task in tasks:
            if not task.done():
                task.cancel()
        if mapping is not None:
            mapping.cancel()

    yield "data: [DONE]\n\n"
