# from pathlib import Path

# from src.dependencies import get_service_container
# from src.schemas.general_review import GeneralReviewRequest, GeneralReviewResponse


# async def general_review(request: GeneralReviewRequest) -> GeneralReviewResponse:
#     """General review function for any custom review between rules and paras."""

#     service_container = get_service_container()
#     llm_service = service_container.azure_openai_model
#     # faiss_store = service_container.faiss_store

#     prompt = Path(r"src\services\prompts\v1\general_review.mustache").read_text()

#     context = {
#         "paragraph": request.paragraph,
#         "rule": request.rule,
#     }

#     response: GeneralReviewResponse = await llm_service.generate(prompt=prompt, context=context, response_model=GeneralReviewResponse)

#     return response

"""
General Review Tool — analyzes contract clauses for apply/dismiss suggestions.

Two modes:

  1. ``clause_review``  — the user selected a specific clause. Runs a
     relevance gate first: if the user's query doesn't apply to that
     clause, we short-circuit with an alert. Otherwise we review the
     clause and return suggestions.

  2. ``full_document_review`` — no selection. Extracts every clause from
     the session using the shared clause extractor, matches clauses to
     the user's prompt via cosine similarity on embeddings, and reviews
     only the matched clauses in parallel. Small documents skip the
     matching step and review every clause.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.dependencies import get_service_container
from src.schemas.general_review import (
    ClauseSuggestionsLLMResponse,
    GeneralReviewResponse,
    PromptSplitLLMResponse,
    RelevanceCheckLLMResponse,
    Suggestion,
)
from src.services.clause_extractor import (
    ClauseUnit,
    extract_all_clauses,
    extract_clauses,
)
from src.services.session_manager import SessionData

logger = logging.getLogger(__name__)

# --- Tunables ----------------------------------------------------------------

# Concurrency cap for the per-clause review fan-out.
MAX_CONCURRENT_EVALS = 5

# Max characters sent in a single per-clause review LLM call. ~40k chars is
# roughly 10k tokens — leaves headroom for the prompt scaffolding and the
# 16k-token output budget.
MAX_CLAUSE_CHARS = 40_000

# Mode-2 clause matching tunables.
#
# If a document has at most this many clauses we skip embedding-based
# matching and review every clause — retrieval on tiny docs is wasted work.
SMALL_DOC_CLAUSE_LIMIT = 8

# Cosine similarity threshold for a clause to count as "matched" to the user's
# prompt. With BGE embeddings (L2-normalized), 0.30 filters out clauses that
# only share a few common words but are not actually about the topic. If no
# clause clears the threshold we return an empty match list and the caller
# emits a "not found in document" finding — much better UX than topping up
# with irrelevant clauses and letting the LLM invent content for them.
MATCH_SIMILARITY_THRESHOLD = 0.30

# Hard cap on the number of clauses we send for per-clause review per
# sub-topic. Kept small on purpose: most user queries target one or two
# real clauses, and reviewing more just produces noise where the LLM
# narrates what other clauses don't contain.
MAX_MATCHED_CLAUSES = 3

# --- Prompt paths ------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_CLAUSE_REVIEW_PROMPT_PATH = _PROMPTS_DIR / "general_review_clause_prompt.mustache"
_RELEVANCE_PROMPT_PATH = _PROMPTS_DIR / "general_review_relevance_check_prompt.mustache"
_PROMPT_SPLITTER_PROMPT_PATH = _PROMPTS_DIR / "general_review_prompt_splitter_prompt.mustache"

_REVIEW_SYSTEM_MESSAGE = (
    "You are an expert Contract Review Analyst for Accorder AI. "
    "Return only apply/dismiss suggestions that are clearly grounded in the clause text "
    "and clearly applicable to the reviewer's instruction. "
    "Every original_text must be an exact verbatim substring of the clause text. "
    "Return ONLY valid JSON matching the schema."
)

_RELEVANCE_SYSTEM_MESSAGE = (
    "You are a gatekeeper for a contract review assistant. " "Decide whether the reviewer's query applies to the selected clause. " "Return ONLY valid JSON matching the schema."
)

_SPLITTER_SYSTEM_MESSAGE = (
    "You are a query planner for a contract review assistant. "
    "Split multi-topic reviewer instructions into atomic sub-instructions so "
    "downstream retrieval can find the right clause for each topic. "
    "Return ONLY valid JSON matching the schema."
)


# --- Session helpers ---------------------------------------------------------


def _get_session(session_id: str) -> SessionData:
    """Retrieve session data or raise ``ValueError``."""
    container = get_service_container()
    session = container.session_manager.get_session(session_id)
    if not session:
        raise ValueError(f"Session '{session_id}' not found or expired.")
    if len(session.chunk_store) == 0:
        raise ValueError("No document ingested in this session.")
    return session


# --- LLM call plumbing -------------------------------------------------------


def _sync_generate_structured(
    llm: Any,
    system_message: str,
    rendered_prompt: str,
    response_model: Any,
) -> Any:
    """Synchronous structured LLM call — used via ``asyncio.to_thread``.

    Generic over the response model so the same plumbing handles the
    per-clause review call and the relevance-gate call.
    """
    response = llm.client.chat.completions.create(
        model=llm.deployment_name,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": rendered_prompt},
        ],
        temperature=0.0,
        max_tokens=16384,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
                "strict": False,
            },
        },
    )

    response_text = response.choices[0].message.content
    if response_text is None:
        raise ValueError("Empty response from LLM model.")

    return response_model.model_validate(json.loads(response_text))


async def _split_prompt_into_subtopics(user_prompt: str) -> List[str]:
    """Break a multi-topic user prompt into atomic sub-instructions.

    Multi-topic prompts (e.g. "make governing law California AND reject
    non-solicitation") produce a single blurry embedding if we hand them
    to the retriever as-is, which causes cosine similarity to bias toward
    whichever topic dominates the wording. The missed topic then never
    gets a per-clause review.

    Splitting the prompt into independent sub-topics lets us run a fresh
    retrieval + review pass for each one, so every ask the user made has
    its own chance to match the right clause in the document.

    Fallbacks:
      - If the splitter LLM call fails for any reason, we fall back to a
        single-element list containing the original prompt verbatim so
        Mode 2 still runs.
      - If the splitter returns an empty list (schema violation), same
        fallback.
    """
    container = get_service_container()
    llm = container.azure_openai_model
    template = _PROMPT_SPLITTER_PROMPT_PATH.read_text(encoding="utf-8")
    rendered = llm.render_prompt_template(
        prompt=template,
        context={"user_prompt": user_prompt},
    )
    try:
        parsed: PromptSplitLLMResponse = await asyncio.to_thread(
            _sync_generate_structured,
            llm,
            _SPLITTER_SYSTEM_MESSAGE,
            rendered,
            PromptSplitLLMResponse,
        )
    except Exception as exc:
        logger.exception("Prompt splitter failed; falling back to full prompt: %s", exc)
        return [user_prompt]

    cleaned = [s.strip() for s in parsed.subtopics if s and s.strip()]
    if not cleaned:
        logger.warning("Prompt splitter returned no subtopics; falling back to full prompt.")
        return [user_prompt]
    return cleaned


async def _run_relevance_check(
    clause_title: str,
    clause_text: str,
    user_prompt: str,
) -> RelevanceCheckLLMResponse:
    """Ask the gate LLM whether the user's query applies to the selected clause."""
    container = get_service_container()
    llm = container.azure_openai_model
    template = _RELEVANCE_PROMPT_PATH.read_text(encoding="utf-8")
    rendered = llm.render_prompt_template(
        prompt=template,
        context={
            "clause_title": clause_title,
            "clause_text": clause_text,
            "user_prompt": user_prompt,
        },
    )
    return await asyncio.to_thread(
        _sync_generate_structured,
        llm,
        _RELEVANCE_SYSTEM_MESSAGE,
        rendered,
        RelevanceCheckLLMResponse,
    )


async def _run_clause_review(
    clause_title: str,
    clause_text: str,
    user_prompt: str,
) -> List[Suggestion]:
    """Run the per-clause review LLM call and return the suggestions it produced.

    Enforces the "verbatim original_text" rule after the call: any suggestion
    whose ``original_text`` is not actually a substring of the clause is
    dropped with a warning. This protects the apply button from ever being
    handed an un-anchored fix.
    """
    container = get_service_container()
    llm = container.azure_openai_model
    template = _CLAUSE_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")
    rendered = llm.render_prompt_template(
        prompt=template,
        context={
            "clause_title": clause_title,
            "clause_text": clause_text,
            "user_prompt": user_prompt,
        },
    )
    parsed: ClauseSuggestionsLLMResponse = await asyncio.to_thread(
        _sync_generate_structured,
        llm,
        _REVIEW_SYSTEM_MESSAGE,
        rendered,
        ClauseSuggestionsLLMResponse,
    )

    valid: List[Suggestion] = []
    for suggestion in parsed.suggestions:
        if not suggestion.original_text or suggestion.original_text not in clause_text:
            logger.warning(
                "Dropping suggestion for clause '%s' — original_text is not a " "verbatim substring of the clause (apply would fail).",
                clause_title,
            )
            continue
        # Force the clause_title to the canonical one we passed in — the model
        # occasionally rewrites it, and the frontend groups suggestions by title.
        valid.append(
            Suggestion(
                clause_title=clause_title,
                reason=suggestion.reason,
                original_text=suggestion.original_text,
                suggested_fix=suggestion.suggested_fix,
            )
        )
    return valid


# --- Clause-list preparation for Mode 2 --------------------------------------


def _truncate_for_review(title: str, text: str) -> str:
    """Trim an oversized clause body so it fits inside the per-call budget.

    We only trim in the rare case where a single extracted clause is larger
    than ``MAX_CLAUSE_CHARS``. Splitting would give multiple sub-suggestions
    that reference only part of the clause, which is worse UX than a single
    pass over the head of the clause.
    """
    if len(text) <= MAX_CLAUSE_CHARS:
        return text
    logger.warning(
        "Clause '%s' is %d chars — truncating to %d for review.",
        title,
        len(text),
        MAX_CLAUSE_CHARS,
    )
    return text[:MAX_CLAUSE_CHARS]


def _clause_display_title(clause: ClauseUnit) -> str:
    """Choose a human-readable title for a clause (falls back to position)."""
    if clause.heading:
        return clause.heading.strip()
    return f"Clause at position {clause.doc_order + 1}"


# --- Mode 2: clause matching -------------------------------------------------


async def _ensure_embeddings_for_clauses(
    clauses: List[ClauseUnit],
    embedding_service: Any,
) -> None:
    """Backfill embeddings for any clause that doesn't have one yet."""
    for clause in clauses:
        if clause.embedding and len(clause.embedding) > 0:
            continue
        clause.embedding = await embedding_service.generate_embeddings(clause.content)


def _cosine_scores(query_vec: List[float], clauses: List[ClauseUnit]) -> np.ndarray:
    """Cosine similarity scores between query and every clause.

    BGE embeddings are already L2-normalised, so cosine reduces to a dot
    product. We normalise defensively in case a different embedding model
    is swapped in later.
    """
    clause_mat = np.array([c.embedding for c in clauses], dtype=np.float32)
    query = np.array(query_vec, dtype=np.float32)

    clause_norms = np.linalg.norm(clause_mat, axis=1, keepdims=True)
    clause_mat = clause_mat / np.maximum(clause_norms, 1e-10)

    query_norm = np.linalg.norm(query)
    query = query / max(query_norm, 1e-10)

    return clause_mat @ query


def _select_matched_clauses(
    clauses: List[ClauseUnit],
    scores: np.ndarray,
) -> List[Tuple[ClauseUnit, float]]:
    """Pick which clauses to review based on similarity scores.

    Strict threshold-only selection — we take the top clauses whose score
    clears ``MATCH_SIMILARITY_THRESHOLD``, capped at ``MAX_MATCHED_CLAUSES``
    and ordered by score (highest first).

    If no clause clears the threshold, we return an empty list. The caller
    then emits a "not found in document" finding for this sub-topic. We
    intentionally do NOT top up with low-scoring clauses — reviewing
    clauses that aren't really about the topic just gives the LLM room
    to generate narration about what the clause doesn't contain.
    """
    indexed = [(i, s) for i, s in enumerate(scores.tolist()) if s >= MATCH_SIMILARITY_THRESHOLD]
    indexed.sort(key=lambda kv: kv[1], reverse=True)
    capped = indexed[:MAX_MATCHED_CLAUSES]
    return [(clauses[i], score) for i, score in capped]


# --- Public API: clause_review -----------------------------------------------


async def clause_review(
    session_id: str,
    clause_text: str,
    user_prompt: str,
    clause_title: str = "Selected Clause",
) -> GeneralReviewResponse:
    """Mode 1 — review a specific clause selected by the user.

    Steps:
      1. Validate the session.
      2. Run the relevance gate. If the query doesn't apply to the clause,
         short-circuit with ``status="clause_query_mismatch"`` and an
         alert message.
      3. Otherwise, run the per-clause review and return the suggestions.
    """
    _get_session(session_id)  # validates session exists and has content

    # Trim grossly oversized selections before any LLM call.
    trimmed_text = _truncate_for_review(clause_title, clause_text)

    # --- Relevance gate ---
    try:
        relevance = await _run_relevance_check(clause_title, trimmed_text, user_prompt)
    except Exception as exc:
        # If the gate itself fails, we don't want to block the user —
        # log loudly and fall through to the review.
        logger.exception("Relevance gate failed; proceeding with review: %s", exc)
        relevance = RelevanceCheckLLMResponse(relevant=True, reason="gate unavailable")

    if not relevance.relevant:
        return GeneralReviewResponse(
            session_id=session_id,
            mode="clause",
            status="clause_query_mismatch",
            alert_message=relevance.reason,
            suggestions=[],
        )

    # --- Main review ---
    suggestions = await _run_clause_review(clause_title, trimmed_text, user_prompt)

    # When the relevance gate passed but the clause produced no suggestions,
    # the selected clause does not actually contain content that needs
    # changing to satisfy the user's ask. Communicate that via alert_message
    # so the user doesn't see a silent empty response and wonder what happened.
    alert_message: Optional[str] = None
    if not suggestions:
        alert_message = "The selected clause does not contain content that needs to change " "to satisfy your query. You may want to check other clauses or " "rephrase your question."

    return GeneralReviewResponse(
        session_id=session_id,
        mode="clause",
        status="ok",
        alert_message=alert_message,
        suggestions=suggestions,
    )


# --- Public API: full_document_review ---------------------------------------


async def _match_clauses_for_subtopic(
    subtopic: str,
    clauses: List[ClauseUnit],
    embedding_service: Any,
    small_doc: bool,
) -> List[Tuple[ClauseUnit, float]]:
    """Pick which clauses to review for a single sub-topic.

    Small documents skip retrieval entirely and review every clause.
    Larger documents embed the sub-topic and use cosine similarity against
    the pre-computed clause embeddings.
    """
    if small_doc:
        return [(c, 1.0) for c in clauses]

    query_vec = await embedding_service.generate_embeddings(subtopic)
    scores = _cosine_scores(query_vec, clauses)
    matched = _select_matched_clauses(clauses, scores)
    logger.info(
        "Subtopic '%s': matched %d of %d clauses (threshold=%.2f, top score=%.3f).",
        subtopic,
        len(matched),
        len(clauses),
        MATCH_SIMILARITY_THRESHOLD,
        float(scores.max()) if len(scores) else 0.0,
    )
    return matched


async def _run_subtopic_review(
    subtopic: str,
    clauses: List[ClauseUnit],
    embedding_service: Any,
    small_doc: bool,
    semaphore: asyncio.Semaphore,
) -> List[Tuple[ClauseUnit, List[Suggestion]]]:
    """Retrieve + review one sub-topic. Returns ``(clause, suggestions)`` pairs.

    Fans the per-clause LLM calls out through a **shared** semaphore so
    that running multiple sub-topics in parallel does not overshoot the
    total per-request concurrency budget.
    """
    matched = await _match_clauses_for_subtopic(subtopic, clauses, embedding_service, small_doc)

    async def _review_one(clause: ClauseUnit) -> List[Suggestion]:
        title = _clause_display_title(clause)
        text = _truncate_for_review(title, clause.content)
        async with semaphore:
            try:
                return await _run_clause_review(title, text, subtopic)
            except Exception:
                logger.exception(
                    "Per-clause review failed for '%s' on subtopic '%s'",
                    title,
                    subtopic,
                )
                return []

    results = await asyncio.gather(*[_review_one(c) for c, _ in matched])
    return [(clause, suggestions) for (clause, _score), suggestions in zip(matched, results)]


async def full_document_review(
    session_id: str,
    user_prompt: str,
) -> GeneralReviewResponse:
    """Mode 2 — review the entire ingested document against the user prompt.

    Pipeline:
      1. Extract every clause from the session (shared extractor).
      2. Split the user prompt into atomic sub-topics (one LLM call).
         Multi-topic prompts like "governing law should be California AND
         reject non-solicitation" get split so each topic gets its own
         retrieval pass. Single-topic prompts return a one-element list.
      3. For each sub-topic: embed it, retrieve matched clauses by cosine
         similarity (or review all clauses if the doc is small), and
         fan out per-clause review in parallel.
      4. Merge suggestions from all sub-topics, deduping by
         (clause_title, original_text) so the same fix is not emitted
         twice when two sub-topics happen to hit the same span.
      5. Sort the final list by clause document order for a natural UX.
    """
    session = _get_session(session_id)
    container = get_service_container()
    embedding_service = container.embedding_service

    # Scope to the most recently ingested document so a session that has
    # accumulated multiple uploads (e.g. for the compare agent) does not
    # leak earlier documents' clauses into a general review of the latest
    # one. Fall back to all clauses if the latest_document_id isn't set
    # (older sessions ingested before this field existed).
    latest_document_id = session.metadata.get("latest_document_id")
    if latest_document_id and latest_document_id in session.documents:
        clauses = extract_clauses(session, latest_document_id)
        logger.info(
            "Scoping full_document_review to latest document '%s' (%d clauses).",
            latest_document_id,
            len(clauses),
        )
    else:
        # clauses = extract_all_clauses(session)

        latest_document_id = session.metadata.get("latest_document_id")
        if latest_document_id and latest_document_id in session.documents:
            clauses = extract_clauses(session, latest_document_id)
            logger.info(
                "Scoping full_document_review to latest document '%s' (%d clauses).",
                latest_document_id,
                len(clauses),
            )
        else:
            clauses = extract_all_clauses(session)

    if not clauses:
        raise ValueError("No clauses could be extracted from the ingested document.")

    small_doc = len(clauses) <= SMALL_DOC_CLAUSE_LIMIT
    if small_doc:
        logger.info(
            "Small document (%d clauses) — reviewing all per sub-topic without matching.",
            len(clauses),
        )
    else:
        # Pre-compute embeddings once; reused across every sub-topic's
        # retrieval pass.
        await _ensure_embeddings_for_clauses(clauses, embedding_service)

    # Split the user prompt into atomic sub-instructions. Single-topic
    # prompts will come back as a one-element list, so this step is
    # harmless for simple queries and load-bearing for compound ones.
    subtopics = await _split_prompt_into_subtopics(user_prompt)
    logger.info("Prompt split into %d sub-topic(s): %s", len(subtopics), subtopics)

    # Shared concurrency cap across all sub-topics and all clauses, so
    # running N sub-topics in parallel doesn't overshoot the per-request
    # LLM concurrency budget.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVALS)

    # Run sub-topics in parallel. Each sub-topic does its own retrieval
    # and per-clause fan-out internally.
    subtopic_outputs = await asyncio.gather(*[_run_subtopic_review(st, clauses, embedding_service, small_doc, semaphore) for st in subtopics])

    # Merge + dedupe.
    #
    # Two sub-topics may hit the same clause (e.g. both liability and
    # indemnity mention the same Limitation clause). Within that clause
    # the LLM may propose the same fix twice. We dedupe suggestions by
    # (clause_title, original_text) to keep the response clean.
    #
    # We also track which sub-topics produced any suggestions at all so
    # we can populate ``alert_message`` with the sub-topics that were not
    # found anywhere in the document — otherwise the user sees empty
    # results and cannot tell whether the agent looked and found nothing
    # or whether something broke.
    seen_suggestion_keys: set = set()
    suggestions_by_order: Dict[int, List[Suggestion]] = {}
    subtopics_with_content: set = set()

    for subtopic_idx, subtopic_result in enumerate(subtopic_outputs):
        for clause, suggestions in subtopic_result:
            for suggestion in suggestions:
                suggestion_key = (suggestion.clause_title, suggestion.original_text)
                if suggestion_key in seen_suggestion_keys:
                    continue
                seen_suggestion_keys.add(suggestion_key)
                suggestions_by_order.setdefault(clause.doc_order, []).append(suggestion)
                subtopics_with_content.add(subtopic_idx)

    # Collect sub-topics that produced nothing anywhere in the document.
    not_found_subtopics: List[str] = [subtopics[idx] for idx in range(len(subtopics)) if idx not in subtopics_with_content]
    for subtopic_text in not_found_subtopics:
        logger.info("Sub-topic '%s' produced no content; marked as not found.", subtopic_text)

    # Build alert_message based on what was / was not found.
    alert_message: Optional[str] = None
    if not_found_subtopics and suggestions_by_order:
        # Partial success — some sub-topics produced suggestions, others
        # were not found. Tell the user what was missing so they aren't
        # left wondering.
        quoted = ", ".join(f'"{s}"' for s in not_found_subtopics)
        alert_message = f"The following topic(s) were not found in this document: {quoted}. " "The other topic(s) you asked about produced the suggestions below."
    elif not_found_subtopics and not suggestions_by_order:
        # Nothing matched anywhere. All sub-topics came back empty.
        quoted = ", ".join(f'"{s}"' for s in not_found_subtopics)
        alert_message = f"No content matching your request was found in this document. " f"Topic(s) checked: {quoted}."

    flat_suggestions: List[Suggestion] = []
    for doc_order in sorted(suggestions_by_order.keys()):
        flat_suggestions.extend(suggestions_by_order[doc_order])

    return GeneralReviewResponse(
        session_id=session_id,
        mode="document",
        status="ok",
        alert_message=alert_message,
        suggestions=flat_suggestions,
    )
