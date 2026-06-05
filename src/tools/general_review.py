# import asyncio
# from pathlib import Path
# from typing import Any, Dict, List, Optional, Tuple

# import numpy as np

# from src.config.logging import get_logger
# from src.core.container import (
#     get_bedrock_model,
#     get_embedding_service,
#     get_session_manager,
# )
# from src.schemas.general_review import (
#     ClauseSuggestionsLLMResponse,
#     GeneralReviewResponse,
#     PromptSplitLLMResponse,
#     RelevanceCheckLLMResponse,
#     Suggestion,
# )
# from src.services.clause_extractor import (
#     ClauseUnit,
#     extract_all_clauses,
#     extract_clauses,
# )
# from src.services.session_manager import SessionData

# logger = get_logger(__name__)

# MAX_CONCURRENT_EVALS = 5

# MAX_CLAUSE_CHARS = 40_000

# SMALL_DOC_CLAUSE_LIMIT = 8

# MATCH_SIMILARITY_THRESHOLD = 0.20

# MAX_MATCHED_CLAUSES = 3

# _PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"

# _CLAUSE_REVIEW_SYSTEM = (_PROMPTS_DIR / "general_review_clause_system.mustache").read_text(encoding="utf-8")
# _CLAUSE_REVIEW_USER = (_PROMPTS_DIR / "general_review_clause_user.mustache").read_text(encoding="utf-8")

# _RELEVANCE_SYSTEM = (_PROMPTS_DIR / "general_review_relevance_system.mustache").read_text(encoding="utf-8")
# _RELEVANCE_USER = (_PROMPTS_DIR / "general_review_relevance_user.mustache").read_text(encoding="utf-8")

# _SPLITTER_SYSTEM = (_PROMPTS_DIR / "general_review_prompt_splitter_system.mustache").read_text(encoding="utf-8")
# _SPLITTER_USER = (_PROMPTS_DIR / "general_review_prompt_splitter_user.mustache").read_text(encoding="utf-8")


# # --- Session helpers ---------------------------------------------------------


# def _get_session(session_id: str) -> SessionData:
#     """Retrieve session data or raise ``ValueError``."""

#     session_manager = get_session_manager()
#     session = session_manager.get_session(session_id)
#     if not session:
#         raise ValueError(f"Session '{session_id}' not found or expired.")
#     if len(session.chunk_store) == 0:
#         raise ValueError("No document ingested in this session.")
#     return session


# # --- LLM call plumbing -------------------------------------------------------


# async def _split_prompt_into_subtopics(user_prompt: str, session_id: str) -> List[str]:
#     """Break a multi-topic user prompt into atomic sub-instructions"""
#     llm = get_bedrock_model()
#     try:
#         parsed: PromptSplitLLMResponse = await llm.generate(
#             prompt=_SPLITTER_USER,
#             context={"user_prompt": user_prompt},
#             response_model=PromptSplitLLMResponse,
#             session_id=session_id,
#             system_message=_SPLITTER_SYSTEM,
#         )
#     except Exception as exc:
#         logger.exception("Prompt splitter failed; falling back to full prompt: %s", exc)
#         return [user_prompt]

#     cleaned = [s.strip() for s in parsed.subtopics if s and s.strip()]
#     if not cleaned:
#         logger.warning("Prompt splitter returned no subtopics; falling back to full prompt.")
#         return [user_prompt]
#     return cleaned


# async def _run_relevance_check(clause_title: str, clause_text: str, user_prompt: str, session_id: str) -> RelevanceCheckLLMResponse:
#     """Ask the gate LLM whether the user's query applies to the selected clause."""
#     llm = get_bedrock_model()
#     response: RelevanceCheckLLMResponse = await llm.generate(
#         prompt=_RELEVANCE_USER,
#         context={
#             "clause_title": clause_title,
#             "clause_text": clause_text,
#             "user_prompt": user_prompt,
#         },
#         response_model=RelevanceCheckLLMResponse,
#         session_id=session_id,
#         system_message=_RELEVANCE_SYSTEM,
#     )
#     return response


# async def _run_clause_review(clause_title: str, clause_text: str, user_prompt: str, session_id: str) -> List[Suggestion]:
#     """Run the per-clause review LLM call and return the suggestions it produced."""

#     llm = get_bedrock_model()
#     parsed: ClauseSuggestionsLLMResponse = await llm.generate(
#         prompt=_CLAUSE_REVIEW_USER,
#         context={
#             "clause_title": clause_title,
#             "clause_text": clause_text,
#             "user_prompt": user_prompt,
#         },
#         response_model=ClauseSuggestionsLLMResponse,
#         system_message=_CLAUSE_REVIEW_SYSTEM,
#         session_id=session_id,
#     )

#     valid: List[Suggestion] = []
#     for suggestion in parsed.suggestions:
#         if not suggestion.original_text or suggestion.original_text not in clause_text:
#             logger.warning(
#                 "Dropping suggestion for clause '%s' — original_text is not a " "verbatim substring of the clause (apply would fail).",
#                 clause_title,
#             )
#             continue
#         # Force the clause_title to the canonical one we passed in — the model
#         # occasionally rewrites it, and the frontend groups suggestions by title.
#         valid.append(
#             Suggestion(
#                 clause_title=clause_title,
#                 reason=suggestion.reason,
#                 original_text=suggestion.original_text,
#                 suggested_fix=suggestion.suggested_fix,
#             )
#         )
#     return valid


# # --- Clause-list preparation for Mode 2 --------------------------------------


# def _truncate_for_review(title: str, text: str) -> str:
#     """Trim an oversized clause body so it fits inside the per-call budget."""

#     if len(text) <= MAX_CLAUSE_CHARS:
#         return text
#     logger.warning(
#         "Clause '%s' is %d chars — truncating to %d for review.",
#         title,
#         len(text),
#         MAX_CLAUSE_CHARS,
#     )
#     return text[:MAX_CLAUSE_CHARS]


# def _clause_display_title(clause: ClauseUnit) -> str:
#     """Choose a human-readable title for a clause (falls back to position)."""

#     if clause.heading:
#         return clause.heading.strip()
#     return f"Clause at position {clause.doc_order + 1}"


# # --- Mode 2: clause matching -------------------------------------------------


# async def _ensure_embeddings_for_clauses(clauses: List[ClauseUnit], embedding_service: Any, session_id: str) -> None:
#     """Backfill embeddings for any clause that doesn't have one yet."""

#     for clause in clauses:
#         if clause.embedding and len(clause.embedding) > 0:
#             continue
#         clause.embedding = await embedding_service.generate_embeddings(text=clause.content, session_id=session_id)


# def _cosine_scores(query_vec: List[float], clauses: List[ClauseUnit]) -> np.ndarray:
#     """Cosine similarity scores between query and every clause."""

#     clause_mat = np.array([c.embedding for c in clauses], dtype=np.float32)
#     query = np.array(query_vec, dtype=np.float32)

#     clause_norms = np.linalg.norm(clause_mat, axis=1, keepdims=True)
#     clause_mat = clause_mat / np.maximum(clause_norms, 1e-10)

#     query_norm = np.linalg.norm(query)
#     query = query / max(query_norm, 1e-10)

#     return clause_mat @ query


# def _select_matched_clauses(clauses: List[ClauseUnit], scores: np.ndarray) -> List[Tuple[ClauseUnit, float]]:
#     """Pick which clauses to review based on similarity scores."""

#     indexed = [(i, s) for i, s in enumerate(scores.tolist()) if s >= MATCH_SIMILARITY_THRESHOLD]
#     indexed.sort(key=lambda kv: kv[1], reverse=True)
#     capped = indexed[:MAX_MATCHED_CLAUSES]
#     return [(clauses[i], score) for i, score in capped]


# # --- Public API: clause_review -----------------------------------------------


# async def clause_review(session_id: str, clause_text: str, user_prompt: str, clause_title: str = "Selected Clause") -> GeneralReviewResponse:
#     """Mode 1 — review a single user-selected clause against the user prompt, with a relevance gate."""

#     _get_session(session_id)  # validates session exists and has content

#     # Trim grossly oversized selections before any LLM call.
#     trimmed_text = _truncate_for_review(clause_title, clause_text)

#     # --- Relevance gate ---
#     try:
#         relevance = await _run_relevance_check(clause_title, trimmed_text, user_prompt, session_id)
#     except Exception as exc:
#         # If the gate itself fails, we don't want to block the user —
#         # log loudly and fall through to the review.
#         logger.exception("Relevance gate failed; proceeding with review: %s", exc)
#         relevance = RelevanceCheckLLMResponse(relevant=True, reason="gate unavailable")

#     if not relevance.relevant:
#         return GeneralReviewResponse(
#             session_id=session_id,
#             mode="clause",
#             status="clause_query_mismatch",
#             alert_message=relevance.reason,
#             suggestions=[],
#         )

#     # --- Main review ---
#     suggestions = await _run_clause_review(clause_title, trimmed_text, user_prompt, session_id)

#     # When the relevance gate passed but the clause produced no suggestions,
#     # the selected clause does not actually contain content that needs
#     # changing to satisfy the user's ask. Communicate that via alert_message
#     # so the user doesn't see a silent empty response and wonder what happened.
#     alert_message: Optional[str] = None
#     if not suggestions:
#         alert_message = "The selected clause does not contain content that needs to change " "to satisfy your query. You may want to check other clauses or " "rephrase your question."

#     return GeneralReviewResponse(
#         session_id=session_id,
#         mode="clause",
#         status="ok",
#         alert_message=alert_message,
#         suggestions=suggestions,
#     )


# # --- Public API: full_document_review ---------------------------------------


# async def _match_clauses_for_subtopic(subtopic: str, clauses: List[ClauseUnit], embedding_service: Any, small_doc: bool, session_id: str) -> List[Tuple[ClauseUnit, float]]:
#     """For a given sub-topic, pick which clauses to review. Returns (clause, score) pairs."""

#     if small_doc:
#         return [(c, 1.0) for c in clauses]

#     query_vec = await embedding_service.generate_embeddings(text=subtopic, session_id=session_id)
#     scores = _cosine_scores(query_vec, clauses)
#     matched = _select_matched_clauses(clauses, scores)

#     top_scored = sorted(
#         [(_clause_display_title(c), float(s)) for c, s in zip(clauses, scores.tolist())],
#         key=lambda kv: kv[1],
#         reverse=True,
#     )[:5]
#     top_5_str = "; ".join(f"{title!r}={score:.3f}" for title, score in top_scored)

#     logger.info(
#         "Subtopic '%s': matched %d of %d clauses (threshold=%.2f). Top 5: %s",
#         subtopic,
#         len(matched),
#         len(clauses),
#         MATCH_SIMILARITY_THRESHOLD,
#         top_5_str,
#     )
#     return matched


# async def _run_subtopic_review(
#     subtopic: str, clauses: List[ClauseUnit], embedding_service: Any, small_doc: bool, semaphore: asyncio.Semaphore, session_id: str
# ) -> List[Tuple[ClauseUnit, List[Suggestion]]]:
#     """For a given sub-topic, run review on the matched clauses and return their suggestions. Returns (clause, suggestions) pairs."""

#     matched = await _match_clauses_for_subtopic(subtopic, clauses, embedding_service, small_doc, session_id)

#     async def _review_one(clause: ClauseUnit) -> List[Suggestion]:
#         title = _clause_display_title(clause)
#         text = _truncate_for_review(title, clause.content)
#         async with semaphore:
#             try:
#                 return await _run_clause_review(title, text, subtopic, session_id)
#             except Exception:
#                 logger.exception(
#                     "Per-clause review failed for '%s' on subtopic '%s'",
#                     title,
#                     subtopic,
#                 )
#                 return []

#     results = await asyncio.gather(*[_review_one(c) for c, _ in matched])
#     return [(clause, suggestions) for (clause, _score), suggestions in zip(matched, results)]


# async def full_document_review(session_id: str, user_prompt: str) -> GeneralReviewResponse:
#     """Mode 2 — review the full document against the user prompt, by splitting the prompt into sub-topics, retrieving relevant clauses for each, and reviewing those clauses. Returns aggregated suggestions across all sub-topics."""

#     session = _get_session(session_id)
#     embedding_service = get_embedding_service()

#     latest_document_id = session.metadata.get("latest_document_id")
#     if latest_document_id and latest_document_id in session.documents:
#         clauses = extract_clauses(session, latest_document_id)
#         logger.info(
#             "Scoping full_document_review to latest document '%s' (%d clauses).",
#             latest_document_id,
#             len(clauses),
#         )
#     else:
#         # clauses = extract_all_clauses(session)

#         latest_document_id = session.metadata.get("latest_document_id")
#         if latest_document_id and latest_document_id in session.documents:
#             clauses = extract_clauses(session, latest_document_id)
#             logger.info(
#                 "Scoping full_document_review to latest document '%s' (%d clauses).",
#                 latest_document_id,
#                 len(clauses),
#             )
#         else:
#             clauses = extract_all_clauses(session)

#     if not clauses:
#         raise ValueError("No clauses could be extracted from the ingested document.")

#     small_doc = len(clauses) <= SMALL_DOC_CLAUSE_LIMIT
#     if small_doc:
#         logger.info(
#             "Small document (%d clauses) — reviewing all per sub-topic without matching.",
#             len(clauses),
#         )
#     else:
#         await _ensure_embeddings_for_clauses(clauses, embedding_service, session_id)

#     subtopics = await _split_prompt_into_subtopics(user_prompt, session_id)
#     logger.info("Prompt split into %d sub-topic(s): %s", len(subtopics), subtopics)

#     semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVALS)

#     subtopic_outputs = await asyncio.gather(*[_run_subtopic_review(st, clauses, embedding_service, small_doc, semaphore, session_id) for st in subtopics])

#     # or whether something broke.
#     seen_suggestion_keys: set = set()
#     suggestions_by_order: Dict[int, List[Suggestion]] = {}
#     subtopics_with_content: set = set()

#     for subtopic_idx, subtopic_result in enumerate(subtopic_outputs):
#         for clause, suggestions in subtopic_result:
#             for suggestion in suggestions:
#                 suggestion_key = (suggestion.clause_title, suggestion.original_text)
#                 if suggestion_key in seen_suggestion_keys:
#                     continue
#                 seen_suggestion_keys.add(suggestion_key)
#                 suggestions_by_order.setdefault(clause.doc_order, []).append(suggestion)
#                 subtopics_with_content.add(subtopic_idx)

#     # Collect sub-topics that produced nothing anywhere in the document.
#     not_found_subtopics: List[str] = [subtopics[idx] for idx in range(len(subtopics)) if idx not in subtopics_with_content]
#     for subtopic_text in not_found_subtopics:
#         logger.info("Sub-topic '%s' produced no content; marked as not found.", subtopic_text)

#     # Build alert_message based on what was / was not found.
#     alert_message: Optional[str] = None
#     if not_found_subtopics and suggestions_by_order:
#         quoted = ", ".join(f'"{s}"' for s in not_found_subtopics)
#         alert_message = f"The following topic(s) were not found in this document: {quoted}. " "The other topic(s) you asked about produced the suggestions below."
#     elif not_found_subtopics and not suggestions_by_order:
#         quoted = ", ".join(f'"{s}"' for s in not_found_subtopics)
#         alert_message = f"No content matching your request was found in this document. " f"Topic(s) checked: {quoted}."

#     flat_suggestions: List[Suggestion] = []
#     for doc_order in sorted(suggestions_by_order.keys()):
#         flat_suggestions.extend(suggestions_by_order[doc_order])

#     return GeneralReviewResponse(
#         session_id=session_id,
#         mode="document",
#         status="ok",
#         alert_message=alert_message,
#         suggestions=flat_suggestions,
#     )


import json
from pathlib import Path
from typing import Any

from src.config.logging import get_logger
from src.core.container import get_bedrock_model
from src.schemas.general_review import GeneralReviewRequest, GeneralReviewResponse

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "general_review"
_GENERAL_REVIEW_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_GENERAL_REVIEW_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")


async def general_review_service(request: GeneralReviewRequest, session_id: str) -> GeneralReviewResponse:
    """Review the clause or the document that user have given."""

    llm_model = get_bedrock_model()

    llm_result: GeneralReviewResponse = await llm_model.generate(
        prompt=_GENERAL_REVIEW_USER,
        context={
            "user_query": request.query,
            "context": [chunk.model_dump() for chunk in request.textinformation],
        },
        response_model=GeneralReviewResponse,
        system_message=_GENERAL_REVIEW_SYSTEM,
        session_id=session_id,
    )

    return llm_result


async def general_review_streaming_service(request: GeneralReviewRequest, session_id: str) -> Any:
    """Review the clause or the document that user have given in streaming mode."""

    llm_model = get_bedrock_model()

    stream = llm_model.generate_stream(
        prompt=_GENERAL_REVIEW_USER,
        context={
            "user_query": request.query,
            "context": [chunk.model_dump() for chunk in request.textinformation],
        },
        system_message=_GENERAL_REVIEW_SYSTEM,
        session_id=session_id,
    )

    async for chunk in stream:
        yield f"data: {json.dumps(chunk)}\n\n"

    yield "data: [DONE]\n\n"
