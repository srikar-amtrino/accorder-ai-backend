"""
Describe & Draft tool — classify intent, generate a draft, validate, write session memory.

Two modes, gated by whether a document is attached to the session:
  - With document: drafts are grounded in extracted parties + governing law and
    checked against the document for duplicate clauses.
  - Without document: drafts are emitted as reusable templates with [PLACEHOLDER]
    tokens (ALL CAPS in square brackets) that the frontend can substitute later.

single_clause mode returns one draft per call; regenerate is supported either
implicitly (regenerate=true on a session with a prior draft) or explicitly via
target_clause_title, which looks up a clause from a prior list_of_clauses
response and regenerates just that one.

All business logic for the Describe & Draft Agent lives here. The agent module
(src/agents/describe_draft.py) is a thin dispatcher that delegates to this module.
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.dependencies import get_service_container
from src.schemas.describe_draft import (
    ClauseListEntry,
    ClauseListLLMResponse,
    ClauseLocation,
    ClauseVersion,
    DescribeDraftErrorType,
    DescribeDraftLLMResponse,
    DescribeDraftResponse,
    DuplicateCheckResult,
    ExistingClauseMatch,
    IntentClassification,
)
from src.services.prompts.v1 import load_prompt

logger = logging.getLogger(__name__)

# Load the static generation system block once at import. It is mode-agnostic
# (covers both single_clause and list_of_clauses) and has no mustache vars, so
# it is the cacheable input. The dynamic per-call payload (mode, agreement
# type, doc grounding, prior draft, user request) goes into the user template
# which is rendered every call.
#
# Token budget: this block is ~5K tokens — at or just above the Opus 4.7
# ephemeral-cache minimum, so `cache_system=True` is worth setting on the
# generation calls. The classifier and duplicate-check prompts are well below
# the threshold and stay single-file with no caching.
_GENERATION_SYSTEM = load_prompt("describe_draft_generation_system")

# Retrieval settings for doc-grounded drafting
_RETRIEVAL_TOP_K = 4
# Minimum similarity score on the top chunk to trigger LLM duplicate confirmation.
# The local MiniLM-L6-v2 embeddings produce cosine scores in roughly [0, 1]
# but the distribution is tighter than OpenAI embeddings — a chunk clearly on the
# same topic typically scores 0.40–0.70. The LLM confirmation call is the real
# accuracy gate, so we keep this threshold low to avoid false negatives.
_DUPLICATE_SIMILARITY_GATE = 0.40

# --- Banned phrase list for post-generation validator ---
_BANNED_PHRASES = [
    "witnesseth",
    "party of the first part",
    "party of the second part",
    "in witness whereof",
    "now therefore",
    "know all men by these presents",
]

# Axis-label patterns that must not leak into titles or summaries (case-insensitive
# substring match). These target phrases the LLM uses when it labels a draft by
# stylistic axis instead of clause content, not legitimate legal vocabulary.
_BANNED_TITLE_SUMMARY_WORDS = [
    "party a-focused",
    "party b-focused",
    "party a-weighted",
    "party b-weighted",
    "weighted toward party",
    "version 1",
    "version 2",
    "version 3",
    "version 4",
    "version 5",
    "balanced version",
    "protective version",
    "plain-english version",
    "plain english version",
    "exhaustive version",
    "comprehensive version",
    "minimal version",
    "essential version",
    "belt-and-suspenders",
    "regenerated version",
    "improved version",
]

# Max prompt length (must match schema Field max_length)
_MAX_PROMPT_LENGTH = 2000

# Injection denylist — checked as case-insensitive substring matches
_INJECTION_PATTERNS = [
    "ignore all instructions",
    "ignore previous instructions",
    "disregard previous",
    "forget your instructions",
    "system prompt:",
    "system: ",
]

# Placeholder token format: [ALL CAPS + SPACES + DIGITS], e.g. [PARTY A], [EFFECTIVE DATE]
_PLACEHOLDER_PATTERN = re.compile(r"\[([A-Z][A-Z0-9 /\-&]{1,60})\]")

# Placeholder token-name substrings that MUST NOT appear when a document is attached.
# Document grounding always supplies the parties and the governing law / forum, so a
# `[PARTY A]` or `[GOVERNING STATE]` token in a doc-grounded draft is a real violation.
# Factual placeholders the document does NOT supply (amounts, dates, durations, cure /
# notice periods) are allowed even in doc-grounded mode — the user must fill those in
# either way. Substring match is case-insensitive against the token name.
_GROUNDED_FORBIDDEN_PLACEHOLDER_SUBSTRINGS = [
    # party-identity tokens
    "PARTY",
    "TENANT",
    "LANDLORD",
    "CUSTOMER",
    "CLIENT",
    "VENDOR",
    "SUPPLIER",
    "EMPLOYER",
    "EMPLOYEE",
    "CONTRACTOR",
    "DISCLOSING",
    "RECEIVING",
    "COMPANY",
    "CORPORATION",
    "BUYER",
    "SELLER",
    "LICENSOR",
    "LICENSEE",
    "INDEMNIFIER",
    "INDEMNITEE",
    # governing-law / forum tokens
    "GOVERNING LAW",
    "GOVERNING STATE",
    "JURISDICTION",
    "VENUE",
    "FORUM",
]
# Minimum body length for an individual drafted clause body in list mode.
# Intentionally permissive: a complete agreement contains some legitimately
# short boilerplate clauses (Headings, Construction, Counterparts, basic
# Severability) that can land at 70-110 chars and still be complete. The 60-char
# per-clause floor only catches truly empty entries ("TBD", "see attached", or
# the LLM accidentally returning the title as the body). Real list-quality
# enforcement happens via the aggregate average gate below — that's what
# guarantees the heavyweight clauses (Indemnification, Limitation of Liability,
# Term & Termination) carry real depth.
_MIN_LIST_CLAUSE_BODY_LEN = 60
# Aggregate floor for list mode: average drafted_clause length across the whole
# list must be ≥ this. This is the PRIMARY quality gate for list mode — it
# ensures the agreement as a whole is substantive without false-positiving the
# legitimately short boilerplate clauses that appear in every well-drafted
# agreement.
_MIN_LIST_AVG_CLAUSE_BODY_LEN = 280
# Single-clause mode: the user is asking for ONE clause and expects industry-grade
# depth. 300 chars is the floor — a complete Governing Law clause with choice of
# law, forum, jury waiver, and CISG carve-out is comfortably > 600 chars.
_MIN_SINGLE_CLAUSE_BODY_LEN = 300
# Summary spec for SINGLE-CLAUSE mode: 2–3 sentences explaining scope, allocation
# of risk, and notable carve-outs. A single-line label fails to brief the reader.
_MIN_SUMMARY_LEN = 80
# Summary floor for LIST mode: the list view is scannable — short one-sentence
# descriptors are appropriate. The body carries the depth, not the summary. 40
# chars catches truly empty labels ("LoL clause") without rejecting useful
# one-liners ("Limits each party's liability for indirect damages.").
_MIN_LIST_SUMMARY_LEN = 40

# Prescribed legal angles cycled through on each successive regenerate of the same
# clause, so the LLM produces visibly different drafts instead of the same shape
# with synonym swaps. These are instructions for the model; they must NOT be
# echoed in titles or summaries (the axis-label validator already enforces that).
_REGENERATE_ANGLES: List[str] = [
    (
        "Take a STRICTER, MORE PROTECTIVE approach than the previous draft: tighter "
        "obligations, fewer carve-outs, shorter cure / notice periods, stronger "
        "remedies (injunctive relief, indemnity for breach). Tilt protection toward "
        "the party receiving the benefit of this clause."
    ),
    (
        "Take a BALANCED, MUTUALLY SYMMETRIC approach: obligations run both ways, "
        "commercially standard carve-outs on both sides, reasonable cure periods, "
        "and remedies available to either party. Rewrite any one-sided language "
        "from the previous draft as symmetric."
    ),
    (
        "Take a PLAIN-ENGLISH, STREAMLINED approach: shorter sentences, modern drafting, "
        "fewer defined terms, fewer commas, active voice. Cut redundancy from the previous "
        "draft. Target roughly 40-60% of its length while preserving the core protections."
    ),
    (
        "Take a COMPREHENSIVE, PROCEDURE-HEAVY approach: add explicit notice procedures, "
        "numbered sub-obligations, an exhaustive carve-out list, escalation / dispute steps, "
        "and survival language. Where the previous draft was high-level, spell the steps "
        "out in detail."
    ),
]

# Note: BedrockModel.generate() does not expose a temperature parameter — regeneration
# variation rests on the prompt itself (the cycled _REGENERATE_ANGLES plus the
# "MUST vary at least THREE axes" instruction in describe_draft_generation_prompt).


def _sanitize_prompt(prompt: str) -> str:
    """Raise ValueError if prompt contains injection patterns; return stripped prompt."""
    p = prompt.strip()
    p_lower = p.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in p_lower:
            raise ValueError(f"Prompt contains disallowed pattern: '{pattern}'")
    return p


def _extract_placeholders(text: str) -> List[str]:
    """Return distinct `[ALL CAPS]` placeholder tokens found in text, in first-seen order."""
    seen: List[str] = []
    for match in _PLACEHOLDER_PATTERN.finditer(text or ""):
        token = f"[{match.group(1)}]"
        if token not in seen:
            seen.append(token)
    return seen


def _grounded_forbidden_placeholders(placeholders: List[str]) -> List[str]:
    """Return placeholders whose token name names a party or the governing law.

    These are the only placeholders that are real violations in doc-grounded mode —
    the document already supplied those values. Factual placeholders for facts the
    document does not contain (amounts, dates, durations, cure / notice periods)
    are kept out of the returned list and treated as acceptable.
    """
    forbidden: List[str] = []
    for tok in placeholders:
        name = tok.strip("[]").upper()
        if any(needle in name for needle in _GROUNDED_FORBIDDEN_PLACEHOLDER_SUBSTRINGS):
            forbidden.append(tok)
    return forbidden


def _validate_draft_response(
    response: DescribeDraftLLMResponse,
    *,
    require_placeholders: bool = False,
    forbid_placeholders: bool = False,
) -> None:
    """Validate single_clause mode output: exactly 1 version containing a full clause.

    Checks:
      - exactly 1 version
      - non-empty title and summary
      - drafted_clause is at least 50 chars and contains no banned phrases or axis labels
      - if require_placeholders: drafted_clause contains at least one [ALL CAPS] token
      - if forbid_placeholders: drafted_clause contains NO [ALL CAPS] tokens

    After validation, `version.placeholders` is rewritten to the authoritative
    list of tokens found in `drafted_clause` — the LLM's own list is advisory.
    """
    if len(response.versions) != 1:
        raise ValueError(f"Expected 1 version, got {len(response.versions)}")
    version = response.versions[0]

    if not version.title or not version.title.strip():
        raise ValueError("Version: title is empty")
    if not version.summary or not version.summary.strip():
        raise ValueError("Version: summary is empty")
    if len(version.summary.strip()) < _MIN_SUMMARY_LEN:
        raise ValueError(
            f"Version: summary is a one-line label ({len(version.summary.strip())} chars); "
            f"the spec requires a 2-3 sentence brief covering scope, allocation of risk, "
            f"and notable carve-outs (≥{_MIN_SUMMARY_LEN} chars)"
        )

    # Axis-label leakage check — titles and summaries must describe content, not style
    title_lower = version.title.lower()
    summary_lower = version.summary.lower()
    for word in _BANNED_TITLE_SUMMARY_WORDS:
        if word in title_lower:
            raise ValueError(
                f"Version: title contains forbidden axis label '{word}'"
            )
        if word in summary_lower:
            raise ValueError(
                f"Version: summary contains forbidden axis label '{word}'"
            )

    if not version.drafted_clause.strip():
        raise ValueError("Version: drafted_clause is empty")
    if len(version.drafted_clause.strip()) < _MIN_SINGLE_CLAUSE_BODY_LEN:
        raise ValueError(
            f"Version: drafted_clause is too short for an industry-grade clause "
            f"({len(version.drafted_clause.strip())} chars; ≥{_MIN_SINGLE_CLAUSE_BODY_LEN} required). "
            f"The clause must satisfy the QUALITY BAR — operative rule plus ancillary "
            f"provisions (notice, cure, exceptions, remedies, survival)."
        )
    lower = version.drafted_clause.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in lower:
            raise ValueError(
                f"Version: banned phrase '{phrase}' found in drafted_clause"
            )

    found_placeholders = _extract_placeholders(version.drafted_clause)
    if require_placeholders and not found_placeholders:
        # Soft preference: most clauses benefit from [PLACEHOLDER] tokens so the
        # frontend can do find-and-replace. But some clauses (Severability,
        # Entire Agreement, Counterparts, basic Force Majeure, basic Waiver) have
        # no user-fillable facts — failing them is a worse UX than accepting a
        # placeholder-free template. Log so we can see how often this happens
        # without blocking the response.
        logger.info(
            "describe_draft single_clause draft contains no [PLACEHOLDER] tokens "
            "in no-doc mode (title=%r) — accepting as boilerplate-style clause",
            version.title,
        )
    if forbid_placeholders and found_placeholders:
        grounded_forbidden = _grounded_forbidden_placeholders(found_placeholders)
        if grounded_forbidden:
            raise ValueError(
                f"Version: drafted_clause contains party-identity or governing-law "
                f"[PLACEHOLDER] tokens that must come from the attached document "
                f"(found {grounded_forbidden[:3]}). Factual placeholders for values "
                f"the document does not supply (amounts, dates, durations) are allowed."
            )
    version.placeholders = found_placeholders


def _validate_regenerated_draft_differs(
    new_version: ClauseVersion, prior_version: ClauseVersion
) -> None:
    """Regenerate must produce a draft meaningfully different from the prior one."""
    if (
        new_version.drafted_clause.strip() == prior_version.drafted_clause.strip()
        or new_version.summary.strip() == prior_version.summary.strip()
    ):
        raise ValueError(
            "Regenerated version is identical to the prior draft — not a meaningful variation"
        )


def _validate_clause_list(
    response: ClauseListLLMResponse,
    *,
    require_placeholders: bool = False,
    forbid_placeholders: bool = False,
) -> None:
    """Validate list_of_clauses mode output: one complete clause list (≥12 clauses).

    Every entry must have a non-empty drafted body; no banned phrases.
    Placeholder rule for no-doc mode is aggregate: at least 60% of clauses
    (floor of 4) must include at least one [PLACEHOLDER] token. A few
    boilerplate-only clauses (Confidentiality, Severability, Entire Agreement,
    etc.) can legitimately lack placeholders and should not fail the whole list.
    For doc-grounded lists, NO clause may contain placeholder tokens.
    """
    if len(response.clauses) < 12:
        raise ValueError(
            f"Expected at least 12 clauses for a complete agreement, "
            f"got {len(response.clauses)}"
        )
    # Agreement summary is the orienting overview shown at the top of the list.
    # Must be non-empty and at least minimally substantive (a one-word stub
    # like "Agreement." is rejected).
    if not response.agreement_summary or not response.agreement_summary.strip():
        raise ValueError("agreement_summary is empty")
    if len(response.agreement_summary.strip()) < 60:
        raise ValueError(
            f"agreement_summary is too short to orient the reader "
            f"({len(response.agreement_summary.strip())} chars; ≥60 required). "
            f"It should be 3-5 sentences covering purpose, parties, core "
            f"exchange, and notable structural features."
        )
    seen_titles: set = set()
    clauses_with_placeholders = 0
    for i, clause in enumerate(response.clauses):
        idx = i + 1
        if not clause.title or not clause.title.strip():
            raise ValueError(f"Clause {idx}: title is empty")
        if not clause.summary or not clause.summary.strip():
            raise ValueError(f"Clause {idx}: summary is empty")
        if len(clause.summary.strip()) < _MIN_LIST_SUMMARY_LEN:
            raise ValueError(
                f"Clause {idx} ('{clause.title}'): summary is too short to be useful "
                f"({len(clause.summary.strip())} chars; ≥{_MIN_LIST_SUMMARY_LEN} required for list mode). "
                f"A short descriptive sentence is fine — the body carries the depth."
            )

        title_norm = clause.title.strip().lower()
        if title_norm in seen_titles:
            raise ValueError(f"Clause {idx}: duplicate title '{clause.title}'")
        seen_titles.add(title_norm)

        # No archaic legalese in summaries
        summary_lower = clause.summary.lower()
        for phrase in _BANNED_PHRASES:
            if phrase in summary_lower:
                raise ValueError(
                    f"Clause {idx}: banned phrase '{phrase}' found in summary"
                )

        # Drafted body checks
        if not clause.drafted_clause or not clause.drafted_clause.strip():
            raise ValueError(f"Clause {idx}: drafted_clause is empty")
        if len(clause.drafted_clause.strip()) < _MIN_LIST_CLAUSE_BODY_LEN:
            raise ValueError(
                f"Clause {idx}: drafted_clause suspiciously short "
                f"({len(clause.drafted_clause.strip())} chars)"
            )
        body_lower = clause.drafted_clause.lower()
        for phrase in _BANNED_PHRASES:
            if phrase in body_lower:
                raise ValueError(
                    f"Clause {idx}: banned phrase '{phrase}' found in drafted_clause"
                )

        found_placeholders = _extract_placeholders(clause.drafted_clause)
        if forbid_placeholders and found_placeholders:
            grounded_forbidden = _grounded_forbidden_placeholders(found_placeholders)
            if grounded_forbidden:
                raise ValueError(
                    f"Clause {idx} ('{clause.title}'): drafted_clause contains "
                    f"party-identity or governing-law [PLACEHOLDER] tokens that "
                    f"must come from the attached document "
                    f"(found {grounded_forbidden[:3]}). Factual placeholders for "
                    f"values the document does not supply are allowed."
                )
        if found_placeholders:
            clauses_with_placeholders += 1
        clause.placeholders = found_placeholders

    if require_placeholders:
        total = len(response.clauses)
        recommended_min = max(4, int(total * 0.6))
        if clauses_with_placeholders < recommended_min:
            # Log only — do not reject. Even when the LLM under-uses placeholders,
            # the drafted clauses are still usable: role labels (Tenant, Vendor,
            # Employee) make the agreement readable, and the user can find-and-
            # replace specific names afterward. Returning nothing because the
            # placeholder count was 4/12 instead of 7/12 is worse than returning
            # a usable draft. The prompt continues to push the LLM toward
            # placeholders; this gate is a soft observation, not a blocker.
            logger.info(
                "describe_draft list mode below recommended placeholder coverage: "
                "%d/%d clauses have [PLACEHOLDER] tokens (recommended ≥%d). "
                "Returning anyway — user can regenerate or find-and-replace.",
                clauses_with_placeholders, total, recommended_min,
            )

    # Aggregate depth — log only, do not reject. A "thin" list is still useful
    # output the user can act on; rejecting it returns nothing, which is worse.
    # The user can always regenerate any specific clause they want deeper.
    total_body_chars = sum(len(c.drafted_clause.strip()) for c in response.clauses)
    avg_body_len = total_body_chars / len(response.clauses)
    if avg_body_len < _MIN_LIST_AVG_CLAUSE_BODY_LEN:
        logger.info(
            "describe_draft list mode below recommended depth: avg=%.0f chars "
            "across %d clauses (recommended ≥%d). Returning anyway — user can "
            "regenerate individual clauses for more depth.",
            avg_body_len, len(response.clauses), _MIN_LIST_AVG_CLAUSE_BODY_LEN,
        )


async def _classify_intent(prompt: str) -> IntentClassification:
    container = get_service_container()
    llm = container.llm_model
    rendered = load_prompt(
        "describe_draft_classifier_prompt", context={"user_prompt": prompt}
    )
    return await llm.generate(
        prompt=rendered,
        context={},
        response_model=IntentClassification,
        mode="JSON",
        system_message="Classify the user's drafting intent. Return ONLY valid JSON.",
    )


def _session_has_document(session) -> bool:
    """True when the session has at least one ingested document."""
    docs = getattr(session, "documents", None)
    if docs:
        return True
    chunk_store = getattr(session, "chunk_store", None)
    return bool(chunk_store)


# Role keywords used to recognise a party-name entry in ContractAnalyzerResponse.key_information.
# The key_information_system prompt instructs the LLM to write specific field names like
# "Licensor Name", "Customer Name", "Disclosing Party", "Tenant", "Landlord" — so a substring
# match on these keywords plus the word "name" / "party" reliably identifies the party rows.
_PARTY_ROLE_KEYWORDS = (
    "party", "licensor", "licensee", "customer", "client", "vendor", "supplier",
    "buyer", "seller", "tenant", "landlord", "employer", "employee", "contractor",
    "consultant", "disclosing", "receiving", "indemnifier", "indemnitee",
    "purchaser", "lessor", "lessee", "service provider",
)

# Keywords that mark a key_information row as the governing-law / jurisdiction value.
_GOVERNING_LAW_KEYWORDS = (
    "governing law", "choice of law", "jurisdiction", "venue", "applicable law", "forum",
)


def _parse_grounding_from_key_info(payload: Any) -> Dict[str, Any]:
    """Pick out parties (name + role) and governing-law text from a ContractAnalyzerResponse.

    The schema does not name these fields explicitly — they live inside a free-form
    `key_information` list of `{field_name, value}` pairs. We keyword-match field
    names against role labels for parties and against governing-law phrases for the
    forum, deduplicating on name so the same party isn't repeated across multiple
    fields (e.g. "Customer Name" and "Customer Billing Address").
    """
    parties: List[Dict[str, Optional[str]]] = []
    gov_law_str = ""
    seen_party_names: set = set()

    key_info = []
    if hasattr(payload, "key_information"):
        key_info = payload.key_information or []
    elif isinstance(payload, dict):
        key_info = payload.get("key_information") or []

    for entry in key_info:
        field_name = (getattr(entry, "field_name", None) or (entry.get("field_name") if isinstance(entry, dict) else "") or "").strip()
        value = (getattr(entry, "value", None) or (entry.get("value") if isinstance(entry, dict) else "") or "").strip()
        if not field_name or not value or value.lower().startswith("absent"):
            continue

        field_lower = field_name.lower()

        if not gov_law_str and any(k in field_lower for k in _GOVERNING_LAW_KEYWORDS):
            gov_law_str = value
            continue

        role_match = next((k for k in _PARTY_ROLE_KEYWORDS if k in field_lower), None)
        if role_match and "name" in field_lower:
            dedupe_key = value.lower()
            if dedupe_key in seen_party_names:
                continue
            seen_party_names.add(dedupe_key)
            parties.append({"name": value, "role": role_match.title()})

    return {"parties": parties, "governing_law": gov_law_str}


async def _get_doc_grounding(session_id: str) -> Optional[Dict[str, Any]]:
    """Extract (or reuse cached) parties + governing_law for the session's document.

    Cached in session.metadata["draft_doc_grounding"] so regenerate calls don't
    re-run the extraction LLM call. Returns None if extraction fails or yields nothing.
    Concatenates session chunk text to feed the Claude key-information agent, then
    parses parties + governing law out of its ContractAnalyzerResponse.
    """
    from src.tools.key_information import get_key_information_document  # local import to avoid cycles

    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    cached = session.metadata.get("draft_doc_grounding")
    if isinstance(cached, dict) and cached.get("parties"):
        return cached

    if not session.chunk_store:
        return None

    ordered_chunks = sorted(session.chunk_store.values(), key=lambda c: getattr(c, "chunk_index", 0))
    content = "\n\n".join(c.content for c in ordered_chunks if getattr(c, "content", None))
    if not content.strip():
        return None

    try:
        payload = await get_key_information_document(content=content, session_id=session_id)
    except Exception as e:
        logger.warning(
            "doc grounding extraction failed session=%s error=%s", session_id, str(e)
        )
        return None

    if not payload:
        return None

    grounding = _parse_grounding_from_key_info(payload)
    if not grounding["parties"] and not grounding["governing_law"]:
        return None

    session.metadata["draft_doc_grounding"] = grounding
    return grounding


async def _retrieve_relevant_chunks(
    session_id: str, query: str, top_k: int = _RETRIEVAL_TOP_K
) -> List[Dict[str, Any]]:
    """Retrieve top-K document chunks matching the drafting prompt. Empty list on failure."""
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    if not _session_has_document(session):
        return []
    try:
        result = await container.retrieval_service.retrieve_data(
            query=query,
            top_k=top_k,
            session_data=session,
        )
    except Exception as e:
        logger.warning(
            "doc retrieval failed session=%s error=%s", session_id, str(e)
        )
        return []
    chunks = result.get("chunks") or []
    return chunks


def _build_clause_location(chunk: Dict[str, Any]) -> Optional[ClauseLocation]:
    """Pull location info (chunk index, section heading, page) from a retrieval chunk."""
    metadata = chunk.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    chunk_index = chunk.get("index")
    if chunk_index is None:
        chunk_index = metadata.get("chunk_index")
    try:
        chunk_index_int = int(chunk_index) if chunk_index is not None else None
    except (TypeError, ValueError):
        chunk_index_int = None

    section_heading = metadata.get("section_heading")
    if isinstance(section_heading, str):
        section_heading = section_heading.strip() or None
    else:
        section_heading = None

    page_raw = metadata.get("page_number") or metadata.get("page")
    try:
        page_number = int(page_raw) if page_raw is not None else None
    except (TypeError, ValueError):
        page_number = None

    if chunk_index_int is None and section_heading is None and page_number is None:
        return None
    return ClauseLocation(
        chunk_index=chunk_index_int,
        section_heading=section_heading,
        page_number=page_number,
    )


_DUPLICATE_CHECK_TOP_N = 3


async def _check_duplicate_clause(
    user_prompt: str, chunks: List[Dict[str, Any]]
) -> Optional[ExistingClauseMatch]:
    """Decide whether any of the top-N retrieved chunks is already a clause on the user's topic.

    A section in the uploaded document is often split across multiple chunks — the
    exact clause the user is asking about might land at rank 2 or 3, not rank 1.
    We iterate the top N chunks (above the similarity gate), run the LLM duplicate
    check on each, and return on the first hit. Each decision point emits an INFO
    log so operators can see exactly why a candidate was or wasn't flagged.
    """
    if not chunks:
        logger.info("duplicate_check skipped — no retrieval chunks")
        return None

    container = get_service_container()

    for rank, chunk in enumerate(chunks[:_DUPLICATE_CHECK_TOP_N]):
        score = float(chunk.get("similarity_score") or 0.0)
        if score < _DUPLICATE_SIMILARITY_GATE:
            logger.info(
                "duplicate_check rank=%d skipped — similarity %.3f below gate %.3f",
                rank, score, _DUPLICATE_SIMILARITY_GATE,
            )
            # Remaining chunks are lower-ranked, so they'll all be below the gate too.
            break

        candidate_text = (chunk.get("content") or "").strip()
        if not candidate_text:
            logger.info("duplicate_check rank=%d skipped — content empty", rank)
            continue

        rendered = load_prompt(
            "describe_draft_duplicate_check_prompt",
            context={"user_request": user_prompt, "candidate_text": candidate_text},
        )
        try:
            result = await container.llm_model.generate(
                prompt=rendered,
                context={},
                response_model=DuplicateCheckResult,
                mode="JSON",
                system_message=(
                    "You decide whether a candidate passage — which may be a full "
                    "clause or only an excerpt — is on the same topic as the user's "
                    "drafting request. Return ONLY valid JSON."
                ),
            )
        except Exception as e:
            logger.warning("duplicate-check LLM call failed rank=%d error=%s", rank, str(e))
            continue

        logger.info(
            "duplicate_check rank=%d similarity=%.3f is_duplicate=%s matched_title=%s",
            rank, score, result.is_duplicate, result.matched_title or "(none)",
        )

        if not result.is_duplicate:
            continue

        inferred_title = result.matched_title
        if not inferred_title:
            metadata = chunk.get("metadata") or {}
            inferred_title = metadata.get("section_heading") if isinstance(metadata, dict) else None

        return ExistingClauseMatch(
            title=inferred_title,
            summary=result.summary,
            drafted_clause=candidate_text,
            similarity_score=score,
            location=_build_clause_location(chunk),
        )

    return None


def _format_parties_block(parties: List[Dict[str, Optional[str]]]) -> str:
    if not parties:
        return ""
    lines = []
    for p in parties:
        role = p.get("role")
        if role:
            lines.append(f"- {p['name']} (role: {role})")
        else:
            lines.append(f"- {p['name']}")
    return "\n".join(lines)


def _format_relevant_chunks(chunks: List[Dict[str, Any]], limit: int = 3) -> str:
    if not chunks:
        return ""
    pieces = []
    for c in chunks[:limit]:
        content = (c.get("content") or "").strip()
        if content:
            pieces.append(content)
    return "\n\n---\n\n".join(pieces)


async def _generate_clause_draft(
    prompt: str,
    agreement_type: Optional[str],
    prior_clauses: List[str],
    prior_draft: Optional[ClauseVersion] = None,
    doc_grounding: Optional[Dict[str, Any]] = None,
    relevant_chunks: Optional[List[Dict[str, Any]]] = None,
    regenerate_angle: Optional[str] = None,
) -> DescribeDraftLLMResponse:
    """single_clause mode: generate exactly 1 draft of the requested clause.

    If `prior_draft` is given, the prompt asks the LLM for a meaningfully
    different and improved variation (regenerate flow); temperature is
    nudged up to encourage variation.

    If `doc_grounding` is given, injects party names + governing law and
    (optionally) relevant chunks from the uploaded document so the draft is
    drop-in ready for that contract. Otherwise the prompt instructs the LLM
    to use `[PLACEHOLDER]` tokens.
    """
    container = get_service_container()
    llm = container.llm_model
    mode_instruction = (
        f"Draft a {agreement_type or 'legal'} clause as requested by the user."
    )
    is_regenerate = prior_draft is not None
    has_grounding = bool(doc_grounding and doc_grounding.get("parties"))
    parties_block = _format_parties_block(doc_grounding["parties"]) if has_grounding else ""
    governing_law = (doc_grounding or {}).get("governing_law") or ""
    chunks_text = _format_relevant_chunks(relevant_chunks or [])

    angle_text = (regenerate_angle or "").strip() if is_regenerate else ""
    context = {
        "user_prompt": prompt,
        "mode": "single_clause",
        "mode_instruction": mode_instruction,
        "is_single_clause": True,
        "is_list_of_clauses": False,
        "agreement_type": agreement_type or "",
        "has_agreement_type": bool(agreement_type),
        "prior_clauses": "\n".join(prior_clauses) if prior_clauses else "",
        "has_prior_clauses": bool(prior_clauses),
        "is_regenerate": is_regenerate,
        "prior_draft_title": prior_draft.title if prior_draft else "",
        "prior_draft_clause": prior_draft.drafted_clause if prior_draft else "",
        "has_regenerate_angle": bool(angle_text),
        "regenerate_angle": angle_text,
        "has_doc_grounding": has_grounding,
        "doc_parties_block": parties_block,
        "doc_governing_law": governing_law or "(not specified in document)",
        "has_relevant_chunks": bool(chunks_text),
        "doc_relevant_chunks": chunks_text,
    }
    rendered = load_prompt("describe_draft_generation_user", context=context)
    return await llm.generate(
        prompt=rendered,
        context={},
        response_model=DescribeDraftLLMResponse,
        mode="JSON",
        system_message=_GENERATION_SYSTEM,
        cache_system=True,
    )


async def _generate_clause_list(
    prompt: str,
    agreement_type: Optional[str],
    prior_clauses: List[str],
    doc_grounding: Optional[Dict[str, Any]] = None,
) -> ClauseListLLMResponse:
    """list_of_clauses mode: return ONE comprehensive clause list with drafted bodies."""
    container = get_service_container()
    llm = container.llm_model
    mode_instruction = (
        f"List all clauses that should appear in a "
        f"{agreement_type or 'legal agreement'} as requested by the user, "
        f"and draft the body of each one."
    )
    has_grounding = bool(doc_grounding and doc_grounding.get("parties"))
    parties_block = _format_parties_block(doc_grounding["parties"]) if has_grounding else ""
    governing_law = (doc_grounding or {}).get("governing_law") or ""

    context = {
        "user_prompt": prompt,
        "mode": "list_of_clauses",
        "mode_instruction": mode_instruction,
        "is_single_clause": False,
        "is_list_of_clauses": True,
        "agreement_type": agreement_type or "",
        "has_agreement_type": bool(agreement_type),
        "prior_clauses": "\n".join(prior_clauses) if prior_clauses else "",
        "has_prior_clauses": bool(prior_clauses),
        "has_doc_grounding": has_grounding,
        "doc_parties_block": parties_block,
        "doc_governing_law": governing_law or "(not specified in document)",
    }
    rendered = load_prompt("describe_draft_generation_user", context=context)
    return await llm.generate(
        prompt=rendered,
        context={},
        response_model=ClauseListLLMResponse,
        mode="JSON",
        system_message=_GENERATION_SYSTEM,
        cache_system=True,
    )


def _read_session_context(session_id: str) -> dict:
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)

    last_raw = session.metadata.get("draft_last_version")
    last_version: Optional[ClauseVersion] = None
    if isinstance(last_raw, dict):
        try:
            last_version = ClauseVersion.model_validate(last_raw)
        except Exception:
            last_version = None

    last_list_raw = session.metadata.get("draft_last_list")
    last_list: List[ClauseListEntry] = []
    if isinstance(last_list_raw, list):
        for entry in last_list_raw:
            if not isinstance(entry, dict):
                continue
            try:
                last_list.append(ClauseListEntry.model_validate(entry))
            except Exception:
                continue

    return {
        "agreement_type": session.metadata.get("draft_agreement_type"),
        "prior_clauses": session.metadata.get("draft_prior_clauses", []) or [],
        "last_version": last_version,
        "last_list": last_list,
    }


def _write_session_context(
    session_id: str,
    agreement_type: Optional[str],
    new_clause_titles: List[str],
    clear_prior: bool = False,
    last_version: Optional[ClauseVersion] = None,
    clear_last_version: bool = False,
    last_list: Optional[List[ClauseListEntry]] = None,
    clear_last_list: bool = False,
) -> None:
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    if agreement_type:
        session.metadata["draft_agreement_type"] = agreement_type
    if clear_prior:
        session.metadata["draft_prior_clauses"] = []
    if new_clause_titles:
        prior = session.metadata.get("draft_prior_clauses", []) or []
        session.metadata["draft_prior_clauses"] = prior + new_clause_titles
    if clear_last_version:
        session.metadata.pop("draft_last_version", None)
    elif last_version is not None:
        session.metadata["draft_last_version"] = last_version.model_dump()
    if clear_last_list:
        session.metadata.pop("draft_last_list", None)
    elif last_list is not None:
        session.metadata["draft_last_list"] = [c.model_dump() for c in last_list]


def _get_regen_count(session_id: str, title: str) -> int:
    """Return how many times the clause with this title has already been regenerated in the session."""
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    counts = session.metadata.get("draft_regenerate_counts") or {}
    if not isinstance(counts, dict):
        return 0
    return int(counts.get(title.strip().lower(), 0))


def _bump_regen_count(session_id: str, title: str) -> None:
    """Increment the regenerate counter for this clause title."""
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    counts = session.metadata.get("draft_regenerate_counts")
    if not isinstance(counts, dict):
        counts = {}
    key = title.strip().lower()
    counts[key] = int(counts.get(key, 0)) + 1
    session.metadata["draft_regenerate_counts"] = counts


def _pick_regenerate_angle(count: int) -> str:
    """Pick the angle for the N-th regenerate of a clause (cycles through the list)."""
    if not _REGENERATE_ANGLES:
        return ""
    return _REGENERATE_ANGLES[count % len(_REGENERATE_ANGLES)]


def _find_clause_in_list(
    clauses: List[ClauseListEntry], title: str
) -> Tuple[Optional[int], Optional[ClauseListEntry]]:
    """Case-insensitive title lookup in a prior list_of_clauses response."""
    target = title.strip().lower()
    if not target:
        return None, None
    for i, entry in enumerate(clauses):
        if entry.title.strip().lower() == target:
            return i, entry
    return None, None


def _error_response(
    session_id: str,
    mode: str,
    error_type: DescribeDraftErrorType,
    message: str,
) -> DescribeDraftResponse:
    return DescribeDraftResponse(
        session_id=session_id,
        mode=mode,  # type: ignore[arg-type]
        status="error",
        disclaimer=None,
        error_type=error_type,
        error_message=message,
    )


async def _run_single_clause_generation(
    session_id: str,
    clean_prompt: str,
    effective_agreement_type: Optional[str],
    prior_clauses: List[str],
    prior_draft_for_prompt: Optional[ClauseVersion],
    doc_grounding: Optional[Dict[str, Any]],
    relevant_chunks: List[Dict[str, Any]],
    is_regenerate: bool,
    regenerate_angle: Optional[str] = None,
) -> Tuple[Optional[ClauseVersion], Optional[DescribeDraftResponse]]:
    """Single-clause generation + validation with one retry.

    Returns (clause_version, None) on success, or (None, error_response) on failure.
    """
    has_grounding = bool(doc_grounding and doc_grounding.get("parties"))
    validation_error: Optional[str] = None

    for attempt in range(2):
        try:
            raw = await _generate_clause_draft(
                prompt=clean_prompt,
                agreement_type=effective_agreement_type,
                prior_clauses=prior_clauses,
                prior_draft=prior_draft_for_prompt,
                doc_grounding=doc_grounding,
                relevant_chunks=relevant_chunks,
                regenerate_angle=regenerate_angle,
            )
            _validate_draft_response(
                raw,
                require_placeholders=not has_grounding,
                forbid_placeholders=has_grounding,
            )
            if is_regenerate and prior_draft_for_prompt is not None:
                _validate_regenerated_draft_differs(
                    raw.versions[0], prior_draft_for_prompt
                )
            return raw.versions[0], None
        except ValueError as ve:
            validation_error = str(ve)
            logger.warning(
                "describe_draft validation failed session=%s attempt=%d regenerate=%s error=%s",
                session_id, attempt + 1, is_regenerate, validation_error,
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(
                "describe_draft generation error session=%s attempt=%d regenerate=%s error=%s",
                session_id, attempt + 1, is_regenerate, error_msg,
            )
            error_type = (
                DescribeDraftErrorType.RATE_LIMITED
                if "rate" in error_msg.lower()
                else DescribeDraftErrorType.LLM_FAILED
            )
            return None, _error_response(
                session_id,
                "single_clause",
                error_type,
                f"LLM generation failed: {error_msg}",
            )

    return None, _error_response(
        session_id,
        "single_clause",
        DescribeDraftErrorType.VALIDATION_FAILED,
        f"Generation validation failed after 2 attempts: {validation_error}",
    )


async def generate_describe_draft(
    prompt: Optional[str],
    session_id: str,
    regenerate: bool = False,
    target_clause_title: Optional[str] = None,
    ignore_document: bool = False,
) -> DescribeDraftResponse:
    """
    Main entry point for the describe-draft agent.

    Flow:
      1. If target_clause_title is set → short-circuit into regenerate-by-title path:
         look up the prior clause body (from draft_last_list or draft_last_version),
         route into single-clause generation with that as prior_draft. Skip classifier.
      2. Otherwise: sanitize input, classify intent.
      3. On clarification: return immediately with no generation.
      4. On list_of_clauses: generate clause list (with drafted bodies), validate, retry once.
         On single_clause: duplicate-check (if doc attached), then generate ONE draft
         (regenerate=True asks for an improved variation), validate, retry once.
      5. Write session memory.
      6. Emit audit log.
    """
    start_time = time.time()

    # --- 1. Regenerate-by-title short-circuit ---
    if target_clause_title and target_clause_title.strip():
        return await _regenerate_by_title(
            session_id=session_id,
            target_clause_title=target_clause_title.strip(),
            refinement_prompt=prompt,
            start_time=start_time,
        )

    # --- 2. Sanitize input ---
    raw_prompt = prompt or ""
    if not raw_prompt.strip():
        return _error_response(
            session_id,
            "clarification",
            DescribeDraftErrorType.VALIDATION_FAILED,
            "Prompt must not be empty when target_clause_title is not provided.",
        )
    try:
        clean_prompt = _sanitize_prompt(raw_prompt)
    except ValueError as e:
        return _error_response(
            session_id,
            "clarification",
            DescribeDraftErrorType.VALIDATION_FAILED,
            str(e),
        )

    # Read session memory
    ctx = _read_session_context(session_id)
    stored_agreement_type: Optional[str] = ctx["agreement_type"]
    prior_clauses: List[str] = ctx["prior_clauses"]
    stored_last_version: Optional[ClauseVersion] = ctx["last_version"]

    # Classify intent
    try:
        classification = await _classify_intent(clean_prompt)
    except Exception as e:
        logger.error(
            "describe_draft classify error session=%s error=%s", session_id, str(e)
        )
        return _error_response(
            session_id,
            "clarification",
            DescribeDraftErrorType.LLM_FAILED,
            f"Intent classification failed: {str(e)}",
        )

    mode = classification.mode
    detected_agreement_type = classification.detected_agreement_type

    # Clarification path — no generation
    if mode == "clarification":
        return DescribeDraftResponse(
            session_id=session_id,
            mode="clarification",
            status="ok",
            clarification_question=classification.clarification_question,
            versions=[],
            error_type=None,
        )

    effective_agreement_type = detected_agreement_type or stored_agreement_type
    clear_prior = (
        detected_agreement_type is not None
        and stored_agreement_type is not None
        and detected_agreement_type.lower() != stored_agreement_type.lower()
    )

    # Detect whether a document is attached (both modes benefit from grounding).
    # When ignore_document=True, force the no-doc code path even if the session
    # has a document — every downstream branch (grounding, retrieval, duplicate
    # check, placeholder validators) already handles has_document=False
    # correctly, so this single short-circuit is the entire opt-out.
    container = get_service_container()
    session_obj = container.session_manager.get_or_create_session(session_id)
    if ignore_document:
        has_document = False
        logger.info(
            "describe_draft session=%s ignore_document=true — forcing no-doc path "
            "even though session may have an attached document",
            session_id,
        )
    else:
        has_document = _session_has_document(session_obj)
    doc_grounding: Optional[Dict[str, Any]] = None
    if has_document:
        doc_grounding = await _get_doc_grounding(session_id)
    grounded_in_doc = bool(doc_grounding and doc_grounding.get("parties"))

    clauses_out: List[ClauseListEntry] = []
    versions_out: List[ClauseVersion] = []
    agreement_summary_out: Optional[str] = None
    units_generated = 0
    validation_error: Optional[str] = None

    if mode == "list_of_clauses":
        list_response: Optional[ClauseListLLMResponse] = None
        for attempt in range(2):
            try:
                raw_list = await _generate_clause_list(
                    prompt=clean_prompt,
                    agreement_type=effective_agreement_type,
                    prior_clauses=prior_clauses if not clear_prior else [],
                    doc_grounding=doc_grounding,
                )
                _validate_clause_list(
                    raw_list,
                    require_placeholders=not grounded_in_doc,
                    forbid_placeholders=grounded_in_doc,
                )
                list_response = raw_list
                break
            except ValueError as ve:
                validation_error = str(ve)
                logger.warning(
                    "describe_draft list validation failed session=%s attempt=%d error=%s",
                    session_id, attempt + 1, validation_error,
                )
            except Exception as e:
                error_msg = str(e)
                logger.error(
                    "describe_draft list generation error session=%s attempt=%d error=%s",
                    session_id, attempt + 1, error_msg,
                )
                error_type = (
                    DescribeDraftErrorType.RATE_LIMITED
                    if "rate" in error_msg.lower()
                    else DescribeDraftErrorType.LLM_FAILED
                )
                return _error_response(
                    session_id,
                    mode,
                    error_type,
                    f"LLM generation failed: {error_msg}",
                )

        if list_response is None:
            return _error_response(
                session_id,
                mode,
                DescribeDraftErrorType.VALIDATION_FAILED,
                f"Clause-list validation failed after 2 attempts: {validation_error}",
            )

        clauses_out = list_response.clauses
        agreement_summary_out = list_response.agreement_summary
        units_generated = len(clauses_out)
    else:
        # single_clause mode — generate ONE draft (regenerate asks for an improved
        # variation of the prior draft stored in session).
        effective_regenerate = regenerate and stored_last_version is not None
        prior_draft_for_prompt = stored_last_version if effective_regenerate else None

        regenerate_angle: Optional[str] = None
        if effective_regenerate and prior_draft_for_prompt is not None:
            regen_count = _get_regen_count(session_id, prior_draft_for_prompt.title)
            regenerate_angle = _pick_regenerate_angle(regen_count)

        relevant_chunks: List[Dict[str, Any]] = []
        if has_document:
            relevant_chunks = await _retrieve_relevant_chunks(session_id, clean_prompt)
            # Duplicate detection — skip when regenerate is active (user already
            # saw the prior draft; they want a new one regardless).
            if not effective_regenerate and relevant_chunks:
                try:
                    existing = await _check_duplicate_clause(clean_prompt, relevant_chunks)
                except Exception as e:
                    logger.warning(
                        "duplicate check errored session=%s error=%s", session_id, str(e)
                    )
                    existing = None
                if existing is not None:
                    # Store the existing clause as if it were the prior draft, so a
                    # subsequent regenerate=true call has something to vary from.
                    # Without this, regenerate finds no stored_last_version, falls
                    # back to a fresh-draft path, hits the duplicate check again,
                    # and returns the same single_clause_exists response forever.
                    section_heading = (
                        existing.location.section_heading
                        if existing.location and existing.location.section_heading
                        else None
                    )
                    seed_title = existing.title or "Existing Clause"
                    # Use the real LLM-produced summary if we have one; fall back
                    # to a synthesized note only when the duplicate-check call did
                    # not return a summary (older prompt cache, etc.). This keeps
                    # the seeded prior_draft useful for regenerate variation and
                    # preserves the actual meaning of the clause.
                    location_suffix = f" under \"{section_heading}\"" if section_heading else ""
                    if existing.summary and existing.summary.strip():
                        seed_summary = (
                            f"{existing.summary.strip()} "
                            f"(Existing clause from the uploaded document{location_suffix}.)"
                        )
                    else:
                        seed_summary = (
                            f"Clause already present in the uploaded document"
                            f"{location_suffix}. Stored as the seed for any "
                            f"regenerate request, which will produce a materially "
                            f"different alternative to this text."
                        )
                    existing_as_version = ClauseVersion(
                        title=seed_title,
                        summary=seed_summary,
                        drafted_clause=existing.drafted_clause,
                        placeholders=[],
                    )

                    # Immediately produce a fresh industry-grade draft seeded by
                    # the existing clause, so the user gets something usable in
                    # the same response. The summary is post-processed to start
                    # with a notice that the clause already exists in the doc,
                    # under what title — the user can then accept, replace, or
                    # regenerate. is_regenerate=True ensures the new draft is
                    # materially different from the existing text; no angle is
                    # passed so the first auto-draft is a clean default rather
                    # than a stylistic variant.
                    auto_version, auto_error = await _run_single_clause_generation(
                        session_id=session_id,
                        clean_prompt=clean_prompt,
                        effective_agreement_type=effective_agreement_type,
                        prior_clauses=prior_clauses if not clear_prior else [],
                        prior_draft_for_prompt=existing_as_version,
                        doc_grounding=doc_grounding,
                        relevant_chunks=relevant_chunks,
                        is_regenerate=True,
                        regenerate_angle=None,
                    )

                    latency_ms = int((time.time() - start_time) * 1000)

                    if auto_version is not None:
                        # Prepend the existing-clause notice to the LLM's summary
                        # so the user sees up front what already exists in the doc.
                        notice = (
                            f"This clause is already present in the uploaded "
                            f"document under the title \"{seed_title}\". "
                            f"The draft below is an alternative you can review — "
                            f"accept it to replace the existing text, or click "
                            f"regenerate for another variation. "
                        )
                        auto_version.summary = notice + auto_version.summary
                        # Save the new draft (not the existing clause) as the
                        # session's prior_draft so subsequent regenerates produce
                        # variations of THIS draft, cycling through angles.
                        _write_session_context(
                            session_id=session_id,
                            agreement_type=effective_agreement_type,
                            new_clause_titles=[],
                            clear_prior=clear_prior,
                            last_version=auto_version,
                        )
                        logger.info(
                            "describe_draft_audit session=%s mode=single_clause_exists "
                            "agreement_type=%s similarity=%.3f auto_drafted=true "
                            "latency_ms=%d",
                            session_id,
                            effective_agreement_type or "unknown",
                            existing.similarity_score,
                            latency_ms,
                        )
                        return DescribeDraftResponse(
                            session_id=session_id,
                            mode="single_clause_exists",
                            status="ok",
                            existing_clause=existing,
                            versions=[auto_version],
                            grounded_in_document=True,
                        )

                    # Auto-draft failed — fall back to returning just the
                    # existing clause, with the existing-clause-as-seed stored so
                    # the user can still try regenerate.
                    _write_session_context(
                        session_id=session_id,
                        agreement_type=effective_agreement_type,
                        new_clause_titles=[],
                        clear_prior=clear_prior,
                        last_version=existing_as_version,
                    )
                    logger.warning(
                        "describe_draft_audit session=%s mode=single_clause_exists "
                        "agreement_type=%s similarity=%.3f auto_drafted=false "
                        "latency_ms=%d — auto-draft failed; returning existing only",
                        session_id,
                        effective_agreement_type or "unknown",
                        existing.similarity_score,
                        latency_ms,
                    )
                    return DescribeDraftResponse(
                        session_id=session_id,
                        mode="single_clause_exists",
                        status="ok",
                        existing_clause=existing,
                        grounded_in_document=True,
                    )

        version, error_resp = await _run_single_clause_generation(
            session_id=session_id,
            clean_prompt=clean_prompt,
            effective_agreement_type=effective_agreement_type,
            prior_clauses=prior_clauses if not clear_prior else [],
            prior_draft_for_prompt=prior_draft_for_prompt,
            doc_grounding=doc_grounding,
            relevant_chunks=relevant_chunks,
            is_regenerate=effective_regenerate,
            regenerate_angle=regenerate_angle,
        )
        if error_resp is not None:
            return error_resp
        versions_out = [version]  # type: ignore[list-item]
        units_generated = 1
        if effective_regenerate and prior_draft_for_prompt is not None:
            _bump_regen_count(session_id, prior_draft_for_prompt.title)

    # --- Write session memory ---
    is_regenerate_hit = (
        mode == "single_clause"
        and regenerate
        and stored_last_version is not None
    )
    if mode == "single_clause" and versions_out and not is_regenerate_hit:
        new_titles = [versions_out[0].title]
    else:
        new_titles = []
    # Don't auto-clear the other mode's stored draft — keep both draft_last_version
    # and draft_last_list around so per-clause regenerate keeps working after the
    # user drafts a fresh single clause on top of a list response, and vice versa.
    _write_session_context(
        session_id=session_id,
        agreement_type=effective_agreement_type,
        new_clause_titles=new_titles,
        clear_prior=clear_prior,
        last_version=versions_out[0] if (mode == "single_clause" and versions_out) else None,
        last_list=clauses_out if mode == "list_of_clauses" and clauses_out else None,
    )

    # --- Audit log ---
    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "describe_draft_audit session=%s mode=%s agreement_type=%s "
        "units_generated=%d regenerate=%s grounded=%s latency_ms=%d",
        session_id,
        mode,
        effective_agreement_type or "unknown",
        units_generated,
        is_regenerate_hit,
        grounded_in_doc,
        latency_ms,
    )

    return DescribeDraftResponse(
        session_id=session_id,
        mode=mode,
        status="ok",
        agreement_summary=agreement_summary_out,
        clauses=clauses_out,
        versions=versions_out,
        regenerated=is_regenerate_hit,
        grounded_in_document=grounded_in_doc,
    )


async def _regenerate_by_title(
    session_id: str,
    target_clause_title: str,
    refinement_prompt: Optional[str],
    start_time: float,
) -> DescribeDraftResponse:
    """Regenerate a specific clause (by title) from the session's last list or single-clause draft."""
    ctx = _read_session_context(session_id)
    stored_agreement_type: Optional[str] = ctx["agreement_type"]
    prior_clauses: List[str] = ctx["prior_clauses"]
    last_version: Optional[ClauseVersion] = ctx["last_version"]
    last_list: List[ClauseListEntry] = ctx["last_list"]

    # Resolve prior clause to regenerate
    prior_from_list_idx: Optional[int] = None
    prior_draft: Optional[ClauseVersion] = None

    if last_list:
        prior_from_list_idx, list_entry = _find_clause_in_list(last_list, target_clause_title)
        if list_entry is not None:
            prior_draft = ClauseVersion(
                title=list_entry.title,
                summary=list_entry.summary,
                drafted_clause=list_entry.drafted_clause,
            )

    if prior_draft is None and last_version is not None:
        if last_version.title.strip().lower() == target_clause_title.strip().lower():
            prior_draft = last_version

    if prior_draft is None:
        return _error_response(
            session_id,
            "single_clause",
            DescribeDraftErrorType.TARGET_NOT_FOUND,
            f"No prior clause found in session memory with title '{target_clause_title}'.",
        )

    # Sanitize optional refinement prompt (used as the user_prompt context)
    refinement_clean = ""
    if refinement_prompt and refinement_prompt.strip():
        try:
            refinement_clean = _sanitize_prompt(refinement_prompt)
        except ValueError as e:
            return _error_response(
                session_id,
                "single_clause",
                DescribeDraftErrorType.VALIDATION_FAILED,
                str(e),
            )

    # The LLM user_prompt: pass refinement if any, else synthesize a retarget instruction.
    user_prompt_for_llm = (
        f"Regenerate the clause titled \"{prior_draft.title}\". "
        + (f"Refinement: {refinement_clean}" if refinement_clean else "")
    ).strip()

    # Doc grounding (same as normal path)
    container = get_service_container()
    session_obj = container.session_manager.get_or_create_session(session_id)
    has_document = _session_has_document(session_obj)
    doc_grounding: Optional[Dict[str, Any]] = None
    relevant_chunks: List[Dict[str, Any]] = []
    if has_document:
        doc_grounding = await _get_doc_grounding(session_id)
        # Retrieve chunks relevant to the clause title (helps style/defined-term reuse).
        retrieval_query = f"{prior_draft.title} {refinement_clean}".strip()
        relevant_chunks = await _retrieve_relevant_chunks(session_id, retrieval_query)
    grounded_in_doc = bool(doc_grounding and doc_grounding.get("parties"))

    regen_count = _get_regen_count(session_id, prior_draft.title)
    regenerate_angle = _pick_regenerate_angle(regen_count)

    version, error_resp = await _run_single_clause_generation(
        session_id=session_id,
        clean_prompt=user_prompt_for_llm,
        effective_agreement_type=stored_agreement_type,
        prior_clauses=prior_clauses,
        prior_draft_for_prompt=prior_draft,
        doc_grounding=doc_grounding,
        relevant_chunks=relevant_chunks,
        is_regenerate=True,
        regenerate_angle=regenerate_angle,
    )
    if error_resp is not None:
        return error_resp
    assert version is not None
    _bump_regen_count(session_id, prior_draft.title)

    # Update session: replace prior list entry in place (if applicable) and update last_version
    updated_list: Optional[List[ClauseListEntry]] = None
    if prior_from_list_idx is not None and last_list:
        last_list[prior_from_list_idx] = ClauseListEntry(
            title=version.title,
            summary=version.summary,
            drafted_clause=version.drafted_clause,
            placeholders=version.placeholders,
        )
        updated_list = last_list

    _write_session_context(
        session_id=session_id,
        agreement_type=stored_agreement_type,
        new_clause_titles=[],
        last_version=version,
        last_list=updated_list,
    )

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "describe_draft_audit session=%s mode=single_clause target_title=%s "
        "regenerate=True grounded=%s latency_ms=%d",
        session_id,
        target_clause_title,
        grounded_in_doc,
        latency_ms,
    )

    return DescribeDraftResponse(
        session_id=session_id,
        mode="single_clause",
        status="ok",
        versions=[version],
        regenerated=True,
        grounded_in_document=grounded_in_doc,
    )
