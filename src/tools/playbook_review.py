import asyncio
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config.logging import get_logger
from src.dependencies import get_service_container
from src.schemas.playbook_review import (
    MissingClausesLLMResponse,
    PlayBookReviewFinalResponse,
    PlayBookReviewLLMResponse,
    PlayBookReviewResponse,
    RuleCheckRequest,
    RuleInfo,
    RuleResult,
    TextInfo,
)
from src.services.llm.azure_openai_model import AzureOpenAIModel

logger = get_logger(__name__)


AGENT_NAME = "playbook_review_agent"

SIMILARITY_PROMPT = Path(r"src\services\prompts\v1\ai_review_prompt_v2.mustache").read_text(encoding="utf-8")

MISSING_CLAUSES_PROMPT = Path(r"src\services\prompts\v1\missing_clauses.mustache").read_text(encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    """Lowercase and strip all punctuation/whitespace for fuzzy matching."""
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def extract_clauses_from_paragraphs(textinformation: List[TextInfo], rule_titles: List[str]) -> Dict[str, List[TextInfo]]:
    """Extracts clauses from the document paragraphs based on rule titles as boundaries."""

    # normalized_title → original_title
    normalized_titles: Dict[str, str] = {_normalize(t): t for t in rule_titles}

    # Initialize empty lists for every rule so callers always get a key
    clause_map: Dict[str, List[TextInfo]] = {title: [] for title in rule_titles}
    current_clause: Optional[str] = None

    for para in textinformation:
        para_norm = _normalize(para.text)
        matched_title: Optional[str] = None

        for norm_title, original_title in normalized_titles.items():
            # Exact match — standalone header paragraph (Format B)
            if para_norm == norm_title:
                matched_title = original_title
                break

            # Para starts with title — merged header+content (Format A)
            if para_norm.startswith(norm_title):
                matched_title = original_title
                break

        if matched_title:
            # This paragraph opens a new clause; include it (it may carry content)
            current_clause = matched_title
            clause_map[current_clause].append(para)
        elif current_clause is not None:
            # Body paragraph — belongs to the currently active clause
            clause_map[current_clause].append(para)
        # else: paragraph appears before any recognized clause header — skip

    # Warn on any rule whose clause was never found in the document
    for title, paras in clause_map.items():
        if not paras:
            logger.warning("Clause extraction: no paragraphs found for rule '%s'. " "Rule will be evaluated against the full document as fallback.", title)

    return clause_map


def _build_reviewed_rules_summary(reviewed: Dict[Tuple[str, str], PlayBookReviewResponse]) -> str:
    """Builds a concise summary of reviewed rules and their statuses for the missing clauses evaluation."""

    lines = []
    for (title, rule_type), review in reviewed.items():
        para_ids = ", ".join(review.content.para_identifiers) or "none"
        lines.append(f"RULE: {title} ({rule_type}) | STATUS: {review.content.status} | PARAS: {para_ids}")
    return "\n".join(lines) if lines else "None"


async def get_missing_clauses(llm_model: AzureOpenAIModel, full_text: str, reviewed_rules_summary: str) -> MissingClausesLLMResponse:
    """Gets missing clauses from the LLM based on the full document text and a summary of reviewed rules."""

    try:
        response: MissingClausesLLMResponse = await llm_model.generate(
            prompt=MISSING_CLAUSES_PROMPT,
            context={
                "data": full_text,
                "reviewed_rules_summary": reviewed_rules_summary,
            },
            response_model=MissingClausesLLMResponse,
        )
        logger.info(f"Missing clauses identified: {len(response.missing_clauses)}")

        return response

    except Exception as exc:
        logger.exception("Missing clauses evaluation failed.")
        return MissingClausesLLMResponse(missing_clauses=[], total_missing=0, summary=f"LLM error: {exc}")


async def _process_rule(
    rule: RuleInfo,
    clause_map: Dict[str, List[TextInfo]],
    full_document: List[TextInfo],
    llm_model: AzureOpenAIModel,
) -> Tuple[Tuple[str, str], PlayBookReviewResponse]:
    """
    Evaluates a single rule against its extracted clause paragraphs.
    """

    matched_paras: List[TextInfo] = clause_map.get(rule.title, [])

    if not matched_paras:
        logger.warning(f"No extracted paragraphs for rule {rule.title}. " "Falling back to full document.")
        matched_paras = full_document

    paragraph_context = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text.strip()}" for p in matched_paras)

    result = RuleResult(
        title=rule.title,
        instruction=rule.instruction,
        description=rule.description,
        paragraphidentifier=",".join(p.paraindetifier for p in matched_paras),
        paragraphcontext=paragraph_context,
        similarity_scores=[],
    )

    current_rule_type = getattr(rule, "rule_type", None) or getattr(rule, "type", None) or "primary"

    try:
        llm_response: PlayBookReviewLLMResponse = await llm_model.generate(
            prompt=SIMILARITY_PROMPT,
            context={
                "rule_title": result.title,
                "rule_instruction": result.instruction,
                "rule_description": result.description,
                "paragraphs": result.paragraphcontext,
                "rule_type": current_rule_type,
            },
            response_model=PlayBookReviewLLMResponse,
        )

    except Exception as exc:
        logger.exception(
            "LLM rule evaluation failed for rule '%s'.",
            rule.title,
        )

        llm_response = PlayBookReviewLLMResponse(
            para_identifiers=[],
            status="Error",
            reason=str(exc),
            suggestion="",
            suggested_fix="",
        )

    review_response = PlayBookReviewResponse(
        rule_type=current_rule_type,
        rule_title=rule.title,
        rule_instruction=rule.instruction,
        rule_description=rule.description,
        content=llm_response,
    )

    return (
        (rule.title, current_rule_type),
        review_response,
    )


async def review_document(
    session_id: str,
    request: RuleCheckRequest,
    force_update_rules: Optional[List[str]] = None,
) -> PlayBookReviewFinalResponse:
    """
    Main entry point for reviewing a document against a set of rules.

    All rules are evaluated independently regardless of:
    - primary
    - fallback1
    - fallback2
    - fallbackN

    Rule types are metadata only and do not affect execution flow.
    """

    force_update_rules = force_update_rules or []

    container = get_service_container()
    llm_model = container.azure_openai_model

    session_data = container.session_manager.get_session(session_id)

    if not session_data:
        return PlayBookReviewFinalResponse(
            rules_review=[],
            missing_clauses=None,
        )

    # Cache disabled
    rules_to_update: List[RuleInfo] = request.rulesinformation

    # Extract clauses once
    # Deduplicate titles because multiple rule variants may share titles
    rule_titles = list({rule.title for rule in rules_to_update})

    clause_map = extract_clauses_from_paragraphs(
        request.textinformation,
        rule_titles,
    )

    logger.info("Clause extraction complete. " f"{sum(1 for paras in clause_map.values() if paras)}/" f"{len(rule_titles)} rules have matched paragraphs.")

    # Evaluate ALL rules independently and concurrently
    updates: List[
        Tuple[
            Tuple[str, str],
            PlayBookReviewResponse,
        ]
    ] = await asyncio.gather(
        *[
            _process_rule(
                rule=rule,
                clause_map=clause_map,
                full_document=request.textinformation,
                llm_model=llm_model,
            )
            for rule in rules_to_update
        ]
    )

    # Preserve ALL rule evaluations
    # including primary/fallback1/fallback2/etc.
    all_reviews: List[PlayBookReviewResponse] = [result for _, result in updates]

    logger.info(
        "Completed evaluation of %s rules.",
        len(all_reviews),
    )

    # Build reviewed rules summary and evaluate missing clauses (no caching)
    reviewed: Dict[Tuple[str, str], PlayBookReviewResponse] = {key: result for key, result in updates}

    full_text = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text}" for p in request.textinformation)
    reviewed_summary = _build_reviewed_rules_summary(reviewed)
    missing_clauses = await get_missing_clauses(llm_model, full_text, reviewed_summary)

    return PlayBookReviewFinalResponse(
        rules_review=all_reviews,
        missing_clauses=missing_clauses,
    )


# async def _process_rule(rule: RuleInfo, clause_map: Dict[str, List[TextInfo]], full_document: List[TextInfo], llm_model: AzureOpenAIModel) -> Tuple[Tuple[str, str], PlayBookReviewResponse]:
#     """Evaluates a single rule against its extracted clause paragraphs."""

#     matched_paras: List[TextInfo] = clause_map.get(rule.title, [])

#     if not matched_paras:
#         logger.warning(f"No extracted paragraphs for rule {rule.title}. Falling back to full document.")
#         matched_paras = full_document

#     paragraph_context = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text.strip()}" for p in matched_paras)

#     result = RuleResult(
#         title=rule.title,
#         instruction=rule.instruction,
#         description=rule.description,
#         paragraphidentifier=",".join(p.paraindetifier for p in matched_paras),
#         paragraphcontext=paragraph_context,
#         similarity_scores=[],
#     )

#     try:
#         llm_response: PlayBookReviewLLMResponse = await llm_model.generate(
#             prompt=SIMILARITY_PROMPT,
#             context={
#                 "rule_title": result.title,
#                 "rule_instruction": result.instruction,
#                 "rule_description": result.description,
#                 "paragraphs": result.paragraphcontext,
#             },
#             response_model=PlayBookReviewLLMResponse,
#         )

#     except Exception as exc:
#         logger.exception("LLM rule evaluation failed for rule '%s'.", rule.title)
#         llm_response = PlayBookReviewLLMResponse(
#             para_identifiers=[],
#             status="Error",
#             reason=str(exc),
#             suggestion="",
#             suggested_fix="",
#         )

#     return (rule.title, rule.type or "primary"), PlayBookReviewResponse(
#         rule_type=rule.type or "primary",
#         rule_title=rule.title,
#         rule_instruction=rule.instruction,
#         rule_description=rule.description,
#         content=llm_response,
#     )


# async def review_document(session_id: str, request: RuleCheckRequest, force_update_rules: Optional[List[str]] = None) -> PlayBookReviewFinalResponse:
#     """Main entry point for reviewing a document against a set of rules. Supports caching and selective re-evaluation of rules based on changes in rule text or document content."""

#     force_update_rules = force_update_rules or []

#     container = get_service_container()
#     llm_model = container.azure_openai_model  # Embeddings no longer needed

#     session_data = container.session_manager.get_session(session_id)
#     if not session_data:
#         return PlayBookReviewFinalResponse(
#             rules_review=[],
#             missing_clauses=None,
#         )

#     # agent_cache = session_data.tool_results.get(AGENT_NAME, {})
#     # # Key by (title, rule_type) to preserve multiple rule variants (primary, fallback, fallback2, etc.)
#     # cached_reviews: Dict[Tuple[str, str], PlayBookReviewResponse] = {(r.rule_title, r.rule_type): r for r in agent_cache.get("rules_review", [])}

#     # # Determine which rules are stale and need re-evaluation
#     # rules_to_update: List[RuleInfo] = []
#     # for rule in request.rulesinformation:
#     #     cache_key = (rule.title, rule.type or "primary")
#     #     cached = cached_reviews.get(cache_key)
#     #     if not cached or rule.title in force_update_rules or cached.rule_description != rule.description or cached.rule_instruction != rule.instruction:
#     #         rules_to_update.append(rule)

#     # # Nothing changed — serve from cache
#     # if not rules_to_update:
#     #     logger.info("All rules up to date in cache. Returning cached results.")
#     #     return PlayBookReviewFinalResponse(
#     #         rules_review=list(cached_reviews.values()),
#     #         missing_clauses=agent_cache.get("missing_clauses"),
#     #     )

#     # Process all rules (cache disabled)
#     rules_to_update: List[RuleInfo] = request.rulesinformation
#     cached_reviews: Dict[Tuple[str, str], PlayBookReviewResponse] = {}

#     # Extract clauses once for all stale rules
#     rule_titles = [rule.title for rule in rules_to_update]
#     clause_map = extract_clauses_from_paragraphs(request.textinformation, rule_titles)

#     logger.info(f"Clause extraction complete. {sum(1 for paras in clause_map.values() if paras)}/{len(rule_titles)} rules have matched paragraphs.")

#     # Process stale rules concurrently
#     updates: List[Tuple[Tuple[str, str], PlayBookReviewResponse]] = await asyncio.gather(
#         *[
#             _process_rule(
#                 rule,
#                 clause_map,
#                 request.textinformation,  # fallback
#                 llm_model,
#             )
#             for rule in rules_to_update
#         ]
#     )

#     cached_reviews.update(dict(updates))

#     # # Recompute missing clauses if document or rules changed
#     # full_text = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text}" for p in request.textinformation)
#     #
#     # doc_hash = _hash(full_text)
#     # rules_hash = _hash("".join(r.title + r.description for r in request.rulesinformation))
#     #
#     # cached_doc_hash = agent_cache.get("doc_hash")
#     # cached_rules_hash = agent_cache.get("rules_hash")
#     #
#     # if doc_hash != cached_doc_hash or rules_hash != cached_rules_hash:
#     #     logger.info("Document or rules changed. Re-evaluating missing clauses.")
#     #     reviewed_summary = _build_reviewed_rules_summary(cached_reviews)
#     #     missing_clauses = await get_missing_clauses(llm_model, full_text, reviewed_summary)
#     # else:
#     #     missing_clauses = agent_cache.get("missing_clauses")

#     # # Persist updated results to session cache
#     # session_data.tool_results[AGENT_NAME] = {
#     #     "rules_review": list(cached_reviews.values()),
#     #     "missing_clauses": missing_clauses,
#     #     "doc_hash": doc_hash,
#     #     "rules_hash": rules_hash,
#     # }

#     # Cache disabled - not persisting results
#     missing_clauses = None

#     return PlayBookReviewFinalResponse(
#         rules_review=list(cached_reviews.values()),
#         missing_clauses=missing_clauses,
#     )
