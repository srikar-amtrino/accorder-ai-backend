# import asyncio
# import hashlib
# import html
# import re
# from pathlib import Path
# from typing import Dict, List, Optional, Tuple

# from src.config.logging import get_logger
# from src.core.container import get_bedrock_model
# from src.schemas.playbook_review import (
#     MissingClausesLLMResponse,
#     PlayBookReviewFinalResponse,
#     PlayBookReviewLLMResponse,
#     PlayBookReviewResponse,
#     RuleCheckRequest,
#     RuleInfo,
#     RuleResult,
#     TextInfo,
# )
# from src.services.llm.base_model import BaseLLMModel

# logger = get_logger(__name__)


# AGENT_NAME = "playbook_review_agent"

# SIMILARITY_SYSTEM_PROMPT = Path(r"src/services/prompts/v1/ai_review_system.mustache").read_text(encoding="utf-8")
# SIMILARITY_USER_PROMPT = Path(r"src/services/prompts/v1/ai_review_user.mustache").read_text(encoding="utf-8")

# MISSING_CLAUSES_PROMPT = Path(r"src/services/prompts/v1/missing_clauses.mustache").read_text(encoding="utf-8")


# def _hash(text: str) -> str:
#     return hashlib.md5(text.encode("utf-8")).hexdigest()


# def _normalize(text: str) -> str:
#     """Lowercase and strip all punctuation/whitespace for fuzzy matching."""
#     return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


# def extract_clauses_from_paragraphs(textinformation: List[TextInfo], rule_titles: List[str], session_id: str) -> Dict[str, List[TextInfo]]:
#     """Extract clause paragraphs from the document for each unique rule title."""

#     # normalised title -> original title
#     normalized_titles: Dict[str, str] = {_normalize(t): t for t in rule_titles}

#     # initialise empty lists for every unique title
#     clause_map: Dict[str, List[TextInfo]] = {title: [] for title in rule_titles}
#     current_clause: Optional[str] = None

#     for para in textinformation:
#         para_norm = _normalize(para.text)
#         matched_title: Optional[str] = None

#         for norm_title, original_title in normalized_titles.items():
#             if para_norm == norm_title or para_norm.startswith(norm_title):
#                 matched_title = original_title
#                 break

#         if matched_title is not None:
#             # This paragraph opens a new (or the same) clause.
#             current_clause = matched_title
#             clause_map[current_clause].append(para)
#         elif current_clause is not None:
#             # Body paragraph — belongs to the active clause.
#             clause_map[current_clause].append(para)
#         # else: before any recognised heading — skip.

#     for title, paras in clause_map.items():
#         if not paras:
#             logger.warning("no paragraphs found for rule rule will be skipped.", rule_title=title, session_id=session_id)

#     return clause_map


# def _build_reviewed_rules_summary(reviewed: Dict[Tuple[str, str], PlayBookReviewResponse]) -> str:
#     """Builds a concise summary of reviewed rules and their statuses for the missing clauses evaluation."""

#     lines = []
#     for (title, rule_type), review in reviewed.items():
#         para_ids = ", ".join(review.content.para_identifiers) or "none"
#         lines.append(f"RULE: {title} ({rule_type}) | STATUS: {review.content.status} | PARAS: {para_ids}")
#     return "\n".join(lines) if lines else "None"


# async def get_missing_clauses(llm_model: BaseLLMModel, full_text: str, reviewed_rules_summary: str, session_id: str) -> MissingClausesLLMResponse:
#     """Gets missing clauses from the LLM based on the full document text and a summary of reviewed rules."""

#     try:
#         response: MissingClausesLLMResponse = await llm_model.generate(
#             prompt=MISSING_CLAUSES_PROMPT,
#             context={
#                 "data": full_text,
#                 "reviewed_rules_summary": reviewed_rules_summary,
#             },
#             response_model=MissingClausesLLMResponse,
#             session_id=session_id,
#         )
#         logger.info("Missing clauses identified", count=len(response.missing_clauses), session_id=session_id)
#         return response

#     except Exception as exc:
#         logger.exception("Missing clauses evaluation failed.", session_id=session_id)
#         return MissingClausesLLMResponse(
#             missing_clauses=[],
#             total_missing=0,
#             summary=f"LLM error: {exc}",
#         )


# async def _process_rule(rule: RuleInfo, clause_map: Dict[str, List[TextInfo]], llm_model: BaseLLMModel, session_id: str) -> Tuple[Tuple[str, str], PlayBookReviewResponse]:
#     """Evaluates a single rule against its extracted clause paragraphs."""

#     current_rule_type = getattr(rule, "rule_type", None) or getattr(rule, "type", None) or "primary"

#     matched_paras: List[TextInfo] = clause_map.get(rule.title, [])

#     if not matched_paras:
#         logger.warning("No clause paragraphs found for rule returning empty result.", rule=rule.title, current_rule_type=current_rule_type, session_id=session_id)
#         llm_response = PlayBookReviewLLMResponse(
#             para_identifiers=[],
#             matched_clause_name="",
#             status="Not Found",
#             reason="",
#             suggestion="",
#             suggested_fix="",
#         )
#         return (rule.title, current_rule_type), PlayBookReviewResponse(
#             rule_type=current_rule_type,
#             rule_title=rule.title,
#             rule_instruction=rule.instruction,
#             rule_description=rule.description,
#             content=llm_response,
#         )

#     paragraph_context = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {html.unescape(p.text).strip()}" for p in matched_paras)

#     result = RuleResult(
#         title=rule.title,
#         instruction=rule.instruction,
#         description=rule.description,
#         paragraphidentifier=",".join(p.paraindetifier for p in matched_paras),
#         paragraphcontext=paragraph_context,
#         similarity_scores=[],
#     )

#     try:
#         generated_response: PlayBookReviewLLMResponse = await llm_model.generate(
#             prompt=SIMILARITY_USER_PROMPT,
#             context={
#                 "rule_title": result.title,
#                 "rule_instruction": result.instruction,
#                 "rule_description": result.description,
#                 "paragraphs": result.paragraphcontext,
#                 "rule_type": current_rule_type,
#             },
#             response_model=PlayBookReviewLLMResponse,
#             session_id=session_id,
#             system_message=SIMILARITY_SYSTEM_PROMPT,
#         )
#         llm_response = generated_response

#     except Exception:
#         logger.exception("LLM rule evaluation failed for rule", rule.title, session_id=session_id)
#         llm_response = PlayBookReviewLLMResponse(
#             para_identifiers=[],
#             matched_clause_name="",
#             status="Not Found",
#             reason="",
#             suggestion="",
#             suggested_fix="",
#         )

#     return (rule.title, current_rule_type), PlayBookReviewResponse(
#         rule_type=current_rule_type,
#         rule_title=rule.title,
#         rule_instruction=rule.instruction,
#         rule_description=rule.description,
#         content=llm_response,
#     )


# async def review_document(session_id: str, request: RuleCheckRequest, force_update_rules: Optional[List[str]] = None) -> PlayBookReviewFinalResponse:
#     """Main entry point for playbook review. Extracts clauses, evaluates rules, and identifies missing clauses."""

#     force_update_rules = force_update_rules or []

#     llm_model = get_bedrock_model()

#     rules_to_update: List[RuleInfo] = request.rulesinformation

#     # Deduplicate titles for extraction — preserving order, ignoring rule_type.
#     # All variants sharing a title will read from the same clause_map entry.
#     unique_titles: List[str] = list(dict.fromkeys(rule.title for rule in rules_to_update))

#     clause_map = extract_clauses_from_paragraphs(
#         request.textinformation,
#         unique_titles,
#         session_id=session_id,
#     )

#     matched_count = sum(1 for paras in clause_map.values() if paras)
#     logger.info("Clause extraction complete.", matched_count=matched_count, total_titles=len(unique_titles), session_id=session_id)

#     # Evaluate all rules (including fallback variants) concurrently.
#     updates: List[Tuple[Tuple[str, str], PlayBookReviewResponse]] = await asyncio.gather(
#         *[
#             _process_rule(
#                 rule=rule,
#                 clause_map=clause_map,
#                 llm_model=llm_model,
#                 session_id=session_id,
#             )
#             for rule in rules_to_update
#         ]
#     )

#     all_reviews: List[PlayBookReviewResponse] = [result for _, result in updates]

#     logger.info("Completed evaluation of %d rules.", len(all_reviews))

#     full_text = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text}" for p in request.textinformation)
#     reviewed_rules_summary = _build_reviewed_rules_summary(dict(updates))
#     missing_clauses = await get_missing_clauses(llm_model, full_text, reviewed_rules_summary, session_id)

#     return PlayBookReviewFinalResponse(
#         rules_review=all_reviews,
#         missing_clauses=missing_clauses,
#     )


import json
import time
from pathlib import Path
from typing import Any

from src.config.logging import get_logger
from src.core.container import get_bedrock_model
from src.schemas.playbook_review import (
    PlayBookReviewFinalResponse,
    PlayBookReviewLLMResponse,
    PlayBookReviewResponse,
    RuleCheckRequest,
)
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3" / "playbook_review"
_PLAYBOOK_REVIEW_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_PLAYBOOK_REVIEW_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")
_PLAYBOOK_REVIEW_STREAM_SYSTEM = (_PROMPTS_DIR / "stream_system.mustache").read_text(encoding="utf-8")

# _PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "missing_clauses"
# _MISSING_CLAUSES_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
# _MISSING_CLAUSES_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")


async def review_document(session_id: str, request: RuleCheckRequest) -> PlayBookReviewLLMResponse:
    """Review a document against a set of rules. Interacts with the LLM to evaluate compliance and returns a structured response."""

    llm_model: BaseLLMModel = get_bedrock_model()

    try:
        # take all the rule and paragraph information and feed to the LLM to get a review response for this clause
        llm_response: PlayBookReviewLLMResponse = await llm_model.generate(
            prompt=_PLAYBOOK_REVIEW_USER,
            context={
                "rules": [
                    {
                        "rule_index": idx,
                        "title": rule.title,
                        "instruction": rule.instruction,
                        "description": rule.description,
                        "rule_type": rule.rule_type,
                    }
                    for idx, rule in enumerate(request.rulesinformation)
                ],
                "paragraphs": [para.model_dump() for para in request.textinformation],
            },
            response_model=PlayBookReviewLLMResponse,
            session_id=session_id,
            system_message=_PLAYBOOK_REVIEW_SYSTEM,
        )

        return PlayBookReviewLLMResponse(results=llm_response.results, missing_clauses=llm_response.missing_clauses)

    except Exception:
        logger.exception("Document review failed with an error.", session_id=session_id)
        return PlayBookReviewLLMResponse(results=[], missing_clauses=[])


async def playbook_review_service(session_id: str, request: RuleCheckRequest) -> PlayBookReviewFinalResponse:
    """Service function for playbook review. Validates input, interacts with the LLM, and returns the final review response."""

    if not request.rulesinformation:
        logger.warning("No rules provided in the request.", session_id=session_id)
        return PlayBookReviewFinalResponse(
            rules_review=[],
            missing_clauses=[],
        )

    try:
        # Call the review_document function to get the LLM response
        review_response: PlayBookReviewLLMResponse = await review_document(session_id, request)

        reviews = []

        for result in review_response.results:

            if result.rule_index < 0 or result.rule_index >= len(request.rulesinformation):
                logger.warning("Received invalid rule index from LLM response, skipping.", rule_index=result.rule_index, session_id=session_id)
                continue

            rule = request.rulesinformation[result.rule_index]

            reviews.append(
                PlayBookReviewResponse(
                    rule_title=rule.title,
                    rule_type=rule.rule_type,
                    rule_instruction=rule.instruction,
                    rule_description=rule.description,
                    content=result,
                )
            )

        # Return the final response
        return PlayBookReviewFinalResponse(
            rules_review=reviews,
            missing_clauses=review_response.missing_clauses,
        )

    except Exception:
        logger.exception("Playbook review failed with an error.", session_id=session_id)
        return PlayBookReviewFinalResponse(
            rules_review=[],
            missing_clauses=[],
        )


# async def playbook_review_service(session_id: str, request: RuleCheckRequest) -> PlayBookReviewFinalResponse:
#     """Service function for playbook review."""

#     if not request.rulesinformation:
#         logger.warning("No rules provided in the request.", session_id=session_id)
#         return PlayBookReviewFinalResponse(
#             rules_review=[],
#             missing_clauses=None,
#         )

#     try:
#         # Run both LLM calls in parallel
#         review_response, missing_clauses_response = await asyncio.gather(
#             review_document(session_id, request),
#             identify_missing_clauses(session_id, request),
#         )

#         reviews = []

#         for result in review_response.results:

#             if result.rule_index < 0 or result.rule_index >= len(request.rulesinformation):
#                 logger.warning("Received invalid rule index from LLM response, skipping.", rule_index=result.rule_index, session_id=session_id)
#                 continue

#             rule = request.rulesinformation[result.rule_index]

#             reviews.append(
#                 PlayBookReviewResponse(
#                     rule_title=rule.title,
#                     rule_type=rule.rule_type,
#                     rule_instruction=rule.instruction,
#                     rule_description=rule.description,
#                     content=result,
#                 )
#             )

#         return PlayBookReviewFinalResponse(
#             rules_review=reviews,
#             missing_clauses=missing_clauses_response,
#         )

#     except Exception:
#         logger.exception("Playbook review failed with an error.", session_id=session_id)

#         return PlayBookReviewFinalResponse(
#             rules_review=[],
#             missing_clauses=None,
#         )


# async def identify_missing_clauses(session_id: str, request: RuleCheckRequest) -> MissingClausesLLMResponse:

#     llm_model: BaseLLMModel = get_bedrock_model()

#     try:
#         response: MissingClausesLLMResponse = await llm_model.generate(
#             prompt=_MISSING_CLAUSES_USER,
#             context={"document_content": [para.model_dump() for para in request.textinformation]},
#             response_model=MissingClausesLLMResponse,
#             session_id=session_id,
#             system_message=_MISSING_CLAUSES_SYSTEM,
#         )

#         return response

#     except Exception:
#         logger.exception("Missing clause review failed.", session_id=session_id)

#         return MissingClausesLLMResponse(missing_clauses=[])


async def playbook_review_stream_service(session_id: str, request: RuleCheckRequest) -> Any:
    """Service function for streaming playbook review."""

    try:
        llm_model = get_bedrock_model()

        # start_time = time.time()
        stream = llm_model.generate_stream(
            prompt=_PLAYBOOK_REVIEW_USER,
            context={
                "rules": [
                    {
                        "rule_index": idx,
                        "title": rule.title,
                        "instruction": rule.instruction,
                        "description": rule.description,
                        "rule_type": rule.rule_type,
                    }
                    for idx, rule in enumerate(request.rulesinformation)
                ],
                "paragraphs": [para.model_dump() for para in request.textinformation],
            },
            session_id=session_id,
            system_message=_PLAYBOOK_REVIEW_STREAM_SYSTEM,
        )

        async for chunk in stream:
            yield f"data: {json.dumps(chunk)}\n\n"

        # yield 'data: {"__section__": "missing_clauses"}\n\n'

        # draft_stream = llm_model.generate_stream(
        #     prompt=_MISSING_CLAUSES_USER,
        #     context={
        #         "document_content": [para.model_dump() for para in request.textinformation],
        #     },
        #     session_id=session_id,
        #     system_message=_MISSING_CLAUSES_SYSTEM,
        # )

        # async for chunk in draft_stream:
        #     yield f"data: {json.dumps(chunk)}\n\n"

        yield "data: [DONE]\n\n"

        # logger.info("playbook review streaming completed", time_taken=time.time() - start_time, session_id=session_id)

    except Exception as exc:
        logger.exception(
            "Playbook review streaming failed.",
            session_id=session_id,
        )

        yield f'data: {json.dumps({"error": str(exc)})}\n\n'


# async def _pump_stream(stream_factory: Callable[[], AsyncGenerator], section: str, queue: asyncio.Queue) -> None:
#     """Consume a stream and push chunks into a shared queue."""
#     try:
#         logger.info("%s: starting stream", section)

#         stream = stream_factory()

#         first_chunk = True
#         async for chunk in stream:
#             if first_chunk:
#                 logger.info("%s: received first chunk", section)
#                 first_chunk = False

#             await queue.put(
#                 {
#                     "__section__": section,
#                     "__data__": chunk,
#                 }
#             )

#     except Exception as exc:
#         logger.exception("%s stream failed", section)
#         await queue.put(
#             {
#                 "__section__": section,
#                 "__error__": str(exc),
#             }
#         )

#     finally:
#         await queue.put(
#             {
#                 "__section__": section,
#                 "__completed__": True,
#             }
#         )


# async def playbook_review_stream_service(session_id: str, request: RuleCheckRequest) -> AsyncGenerator:
#     """Run rules review and missing clauses streams concurrently."""
#     try:
#         start_time = time.time()

#         rules_context = {
#             "rules": [
#                 {
#                     "rule_index": idx,
#                     "title": rule.title,
#                     "instruction": rule.instruction,
#                     "description": rule.description,
#                     "rule_type": rule.rule_type,
#                 }
#                 for idx, rule in enumerate(request.rulesinformation)
#             ],
#             "paragraphs": [para.model_dump() for para in request.textinformation],
#         }

#         missing_context = {
#             "document_content": [para.model_dump() for para in request.textinformation],
#         }

#         # Use separate model instances to avoid any hidden locks
#         review_model = get_bedrock_model()
#         missing_model = get_bedrock_model()

#         queue: asyncio.Queue = asyncio.Queue()

#         async def review_stream() -> AsyncGenerator:
#             async for chunk in review_model.generate_stream(
#                 prompt=_PLAYBOOK_REVIEW_USER,
#                 context=rules_context,
#                 session_id=session_id,
#                 system_message=_PLAYBOOK_REVIEW_SYSTEM,
#             ):
#                 yield chunk

#         async def missing_stream() -> AsyncGenerator:
#             async for chunk in missing_model.generate_stream(
#                 prompt=_MISSING_CLAUSES_USER,
#                 context=missing_context,
#                 session_id=session_id,
#                 system_message=_MISSING_CLAUSES_SYSTEM,
#             ):
#                 yield chunk

#         review_task = asyncio.create_task(_pump_stream(review_stream, "rules_review", queue))

#         missing_task = asyncio.create_task(_pump_stream(missing_stream, "missing_clauses", queue))

#         completed = 0

#         while completed < 2:
#             item = await queue.get()

#             if item.get("__completed__"):
#                 completed += 1
#                 continue

#             if "__error__" in item:
#                 logger.error("stream error", section=item["__section__"], error=item["__error__"], session_id=session_id)
#                 continue

#             yield f"data: {json.dumps(item)}\n\n"

#         await asyncio.gather(review_task, missing_task, return_exceptions=True)

#         logger.info("playbook review streaming completed", session_id=session_id, time_taken=time.time() - start_time)

#         yield "data: [DONE]\n\n"

#     except Exception as exc:
#         logger.exception("Playbook review streaming failed", session_id=session_id)
#         yield f'data: {json.dumps({"error": str(exc)})}\n\n'
