"""
Describe & Draft tool — classify intent, generate a draft, validate.

Two modes, gated by the `use_document_context` toggle and whether a document is
attached to the session:
  - With document context: the full text of the uploaded document is fed to the
    model so the draft is grounded in its parties and governing law.
  - Without document context: drafts are emitted as reusable templates with
    [PLACEHOLDER] tokens (ALL CAPS in square brackets) that the frontend can
    substitute later.

The classifier routes each prompt to single_clause (one clause requested) or
list_of_clauses (a whole agreement requested). Each call is independent — there
is no regenerate flow and no cross-call session memory.

All business logic for the Describe & Draft Agent lives here. The agent module
(src/agents/describe_draft.py) is a thin dispatcher that delegates to this module.
"""
import logging
import re
import time
from typing import List, Optional, Tuple

from src.dependencies import get_service_container
from src.schemas.describe_draft import (
    ClauseListEntry,
    ClauseListLLMResponse,
    ClauseVersion,
    DescribeDraftErrorType,
    DescribeDraftLLMResponse,
    DescribeDraftResponse,
    IntentClassification,
)
from src.services.prompts.v1 import load_prompt

logger = logging.getLogger(__name__)

# Load the static generation system block once at import. It is mode-agnostic
# (covers both single_clause and list_of_clauses) and has no mustache vars, so
# it is the cacheable input. The dynamic per-call payload (mode, agreement
# type, document text, user request) goes into the user template which is
# rendered every call.
#
# Token budget: this block is ~10K tokens — far above Sonnet 4.6's 1,024-token
# ephemeral-cache minimum, so `cache_system=True` is worth setting on the
# generation calls. The classifier prompt is well below the threshold and stays
# single-file with no caching.
_GENERATION_SYSTEM = load_prompt("describe_draft_generation_system")

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
    "balanced version",
    "protective version",
    "plain-english version",
    "plain english version",
    "exhaustive version",
    "comprehensive version",
    "minimal version",
    "essential version",
    "belt-and-suspenders",
]

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

# Placeholder token-name substrings that MUST NOT appear when document context is used.
# A grounded draft reads the parties and the governing law / forum straight from the
# document, so a `[PARTY A]` or `[GOVERNING STATE]` token in a grounded draft is a real
# violation. Factual placeholders the document does NOT supply (amounts, dates,
# durations, cure / notice periods) are allowed even in grounded mode — the user must
# fill those in either way. Substring match is case-insensitive against the token name.
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
# Minimum body length for an individual drafted clause body in list mode. Intentionally
# permissive: a complete agreement contains some legitimately short boilerplate clauses
# (Headings, Construction, Counterparts, basic Severability) that can land at 70-110
# chars and still be complete. The 60-char per-clause floor only catches truly empty
# entries. Real list-quality enforcement happens via the aggregate average gate below.
_MIN_LIST_CLAUSE_BODY_LEN = 60
# Aggregate floor for list mode: average drafted_clause length across the whole list.
_MIN_LIST_AVG_CLAUSE_BODY_LEN = 280
# Single-clause mode floor — the user is asking for ONE clause and expects depth.
_MIN_SINGLE_CLAUSE_BODY_LEN = 300
# Summary spec for SINGLE-CLAUSE mode: 2–3 sentences explaining scope, allocation of
# risk, and notable carve-outs. A single-line label fails to brief the reader.
_MIN_SUMMARY_LEN = 80
# Summary floor for LIST mode: the list view is scannable, so one-sentence descriptors
# are appropriate. The body carries the depth, not the summary.
_MIN_LIST_SUMMARY_LEN = 40


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

    These are the only placeholders that are real violations in document-grounded
    mode — the document already supplied those values. Factual placeholders for facts
    the document does not contain (amounts, dates, durations, cure / notice periods)
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

    Checks: exactly 1 version; non-empty title and substantive summary; a drafted_clause
    that meets the length floor and contains no banned phrases or axis labels; and the
    placeholder rule for the active mode. After validation, `version.placeholders` is
    rewritten to the authoritative list of tokens found in `drafted_clause`.
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
            raise ValueError(f"Version: title contains forbidden axis label '{word}'")
        if word in summary_lower:
            raise ValueError(f"Version: summary contains forbidden axis label '{word}'")

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
            raise ValueError(f"Version: banned phrase '{phrase}' found in drafted_clause")

    found_placeholders = _extract_placeholders(version.drafted_clause)
    if require_placeholders and not found_placeholders:
        # Soft preference: most clauses benefit from [PLACEHOLDER] tokens so the
        # frontend can do find-and-replace. But some clauses (Severability, Entire
        # Agreement, Counterparts, basic Force Majeure, basic Waiver) have no
        # user-fillable facts — failing them is a worse UX than accepting a
        # placeholder-free template. Log rather than reject.
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


def _validate_clause_list(
    response: ClauseListLLMResponse,
    *,
    require_placeholders: bool = False,
    forbid_placeholders: bool = False,
) -> None:
    """Validate list_of_clauses mode output: one complete clause list (≥12 clauses).

    Every entry must have a non-empty drafted body; no banned phrases. Placeholder
    rule for no-doc mode is aggregate (logged, not enforced). For document-grounded
    lists, NO clause may contain party-identity or governing-law placeholder tokens.
    """
    if len(response.clauses) < 12:
        raise ValueError(
            f"Expected at least 12 clauses for a complete agreement, "
            f"got {len(response.clauses)}"
        )
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
                raise ValueError(f"Clause {idx}: banned phrase '{phrase}' found in summary")

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
                raise ValueError(f"Clause {idx}: banned phrase '{phrase}' found in drafted_clause")

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
            # the drafted clauses are still usable and the user can find-and-replace.
            logger.info(
                "describe_draft list mode below recommended placeholder coverage: "
                "%d/%d clauses have [PLACEHOLDER] tokens (recommended ≥%d). Returning anyway.",
                clauses_with_placeholders, total, recommended_min,
            )

    # Aggregate depth — log only, do not reject. A "thin" list is still usable output.
    total_body_chars = sum(len(c.drafted_clause.strip()) for c in response.clauses)
    avg_body_len = total_body_chars / len(response.clauses)
    if avg_body_len < _MIN_LIST_AVG_CLAUSE_BODY_LEN:
        logger.info(
            "describe_draft list mode below recommended depth: avg=%.0f chars "
            "across %d clauses (recommended ≥%d). Returning anyway.",
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


def _get_document_text(session_id: str) -> Optional[str]:
    """Return the full text of the session's uploaded document, or None if absent.

    Concatenates the session chunk store in chunk order. This is the full document
    content the model is grounded in when document context is on.
    """
    container = get_service_container()
    session = container.session_manager.get_or_create_session(session_id)
    chunk_store = getattr(session, "chunk_store", None)
    if not chunk_store:
        return None
    ordered_chunks = sorted(
        chunk_store.values(), key=lambda c: getattr(c, "chunk_index", 0)
    )
    content = "\n\n".join(
        c.content for c in ordered_chunks if getattr(c, "content", None)
    )
    return content.strip() or None


async def _generate_clause_draft(
    prompt: str,
    agreement_type: Optional[str],
    document_text: Optional[str] = None,
) -> DescribeDraftLLMResponse:
    """single_clause mode: generate exactly 1 draft of the requested clause.

    If `document_text` is given, the full document is injected so the draft is
    grounded in its parties and governing law. Otherwise the prompt instructs the
    LLM to use `[PLACEHOLDER]` tokens.
    """
    container = get_service_container()
    llm = container.llm_model
    mode_instruction = (
        f"Draft a {agreement_type or 'legal'} clause as requested by the user."
    )
    has_document_context = bool(document_text)

    context = {
        "user_prompt": prompt,
        "mode": "single_clause",
        "mode_instruction": mode_instruction,
        "is_single_clause": True,
        "is_list_of_clauses": False,
        "agreement_type": agreement_type or "",
        "has_agreement_type": bool(agreement_type),
        "has_document_context": has_document_context,
        "document_text": document_text or "",
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
    document_text: Optional[str] = None,
) -> ClauseListLLMResponse:
    """list_of_clauses mode: return ONE comprehensive clause list with drafted bodies."""
    container = get_service_container()
    llm = container.llm_model
    mode_instruction = (
        f"List all clauses that should appear in a "
        f"{agreement_type or 'legal agreement'} as requested by the user, "
        f"and draft the body of each one."
    )
    has_document_context = bool(document_text)

    context = {
        "user_prompt": prompt,
        "mode": "list_of_clauses",
        "mode_instruction": mode_instruction,
        "is_single_clause": False,
        "is_list_of_clauses": True,
        "agreement_type": agreement_type or "",
        "has_agreement_type": bool(agreement_type),
        "has_document_context": has_document_context,
        "document_text": document_text or "",
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
    agreement_type: Optional[str],
    document_text: Optional[str],
    grounded: bool,
) -> Tuple[Optional[ClauseVersion], Optional[DescribeDraftResponse]]:
    """Single-clause generation + validation with one retry.

    Returns (clause_version, None) on success, or (None, error_response) on failure.
    """
    validation_error: Optional[str] = None

    for attempt in range(2):
        try:
            raw = await _generate_clause_draft(
                prompt=clean_prompt,
                agreement_type=agreement_type,
                document_text=document_text,
            )
            _validate_draft_response(
                raw,
                require_placeholders=not grounded,
                forbid_placeholders=grounded,
            )
            return raw.versions[0], None
        except ValueError as ve:
            validation_error = str(ve)
            logger.warning(
                "describe_draft validation failed session=%s attempt=%d error=%s",
                session_id, attempt + 1, validation_error,
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(
                "describe_draft generation error session=%s attempt=%d error=%s",
                session_id, attempt + 1, error_msg,
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
    use_document_context: bool = True,
) -> DescribeDraftResponse:
    """
    Main entry point for the describe-draft agent.

    Flow:
      1. Sanitize input, classify intent (single_clause or list_of_clauses).
      2. If use_document_context and a document is attached, load the full document text.
      3. Generate (clause list with drafted bodies, or one clause draft), validate,
         retry once.
      4. Emit audit log and return.
    """
    start_time = time.time()

    raw_prompt = prompt or ""
    if not raw_prompt.strip():
        return _error_response(
            session_id,
            "single_clause",
            DescribeDraftErrorType.VALIDATION_FAILED,
            "Prompt must not be empty.",
        )
    try:
        clean_prompt = _sanitize_prompt(raw_prompt)
    except ValueError as e:
        return _error_response(
            session_id,
            "single_clause",
            DescribeDraftErrorType.VALIDATION_FAILED,
            str(e),
        )

    # Classify intent
    try:
        classification = await _classify_intent(clean_prompt)
    except Exception as e:
        logger.error(
            "describe_draft classify error session=%s error=%s", session_id, str(e)
        )
        return _error_response(
            session_id,
            "single_clause",
            DescribeDraftErrorType.LLM_FAILED,
            f"Intent classification failed: {str(e)}",
        )

    mode = classification.mode
    agreement_type = classification.detected_agreement_type

    # Load the full document only when the user asked for document context AND a
    # document is actually attached to the session.
    container = get_service_container()
    session_obj = container.session_manager.get_or_create_session(session_id)
    document_text: Optional[str] = None
    if use_document_context and _session_has_document(session_obj):
        document_text = _get_document_text(session_id)
        if document_text is None:
            logger.info(
                "describe_draft session=%s use_document_context=true but no document "
                "text could be loaded — falling back to no-doc template mode",
                session_id,
            )
    grounded = bool(document_text)

    if mode == "list_of_clauses":
        list_response: Optional[ClauseListLLMResponse] = None
        validation_error: Optional[str] = None
        for attempt in range(2):
            try:
                raw_list = await _generate_clause_list(
                    prompt=clean_prompt,
                    agreement_type=agreement_type,
                    document_text=document_text,
                )
                _validate_clause_list(
                    raw_list,
                    require_placeholders=not grounded,
                    forbid_placeholders=grounded,
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
                    session_id, mode, error_type, f"LLM generation failed: {error_msg}"
                )

        if list_response is None:
            return _error_response(
                session_id,
                mode,
                DescribeDraftErrorType.VALIDATION_FAILED,
                f"Clause-list validation failed after 2 attempts: {validation_error}",
            )

        latency_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "describe_draft_audit session=%s mode=list_of_clauses agreement_type=%s "
            "units_generated=%d grounded=%s latency_ms=%d",
            session_id,
            agreement_type or "unknown",
            len(list_response.clauses),
            grounded,
            latency_ms,
        )
        return DescribeDraftResponse(
            session_id=session_id,
            mode="list_of_clauses",
            status="ok",
            agreement_summary=list_response.agreement_summary,
            clauses=list_response.clauses,
            grounded_in_document=grounded,
        )

    # single_clause mode
    version, error_resp = await _run_single_clause_generation(
        session_id=session_id,
        clean_prompt=clean_prompt,
        agreement_type=agreement_type,
        document_text=document_text,
        grounded=grounded,
    )
    if error_resp is not None:
        return error_resp

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "describe_draft_audit session=%s mode=single_clause agreement_type=%s "
        "units_generated=1 grounded=%s latency_ms=%d",
        session_id,
        agreement_type or "unknown",
        grounded,
        latency_ms,
    )
    return DescribeDraftResponse(
        session_id=session_id,
        mode="single_clause",
        status="ok",
        versions=[version],  # type: ignore[list-item]
        grounded_in_document=grounded,
    )
