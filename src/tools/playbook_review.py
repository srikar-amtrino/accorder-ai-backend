import asyncio
import hashlib
import html
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config.logging import get_logger
from src.core.container import get_bedrock_model
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
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)


AGENT_NAME = "playbook_review_agent"

SIMILARITY_SYSTEM_PROMPT = Path(r"src/services/prompts/v1/ai_review_system.mustache").read_text(encoding="utf-8")
SIMILARITY_USER_PROMPT = Path(r"src/services/prompts/v1/ai_review_user.mustache").read_text(encoding="utf-8")

MISSING_CLAUSES_PROMPT = Path(r"src/services/prompts/v1/missing_clauses.mustache").read_text(encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    """Lowercase and strip all punctuation/whitespace for fuzzy matching."""
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def extract_clauses_from_paragraphs(textinformation: List[TextInfo], rule_titles: List[str], session_id: str) -> Dict[str, List[TextInfo]]:
    """Extract clause paragraphs from the document for each unique rule title."""

    # normalised title -> original title
    normalized_titles: Dict[str, str] = {_normalize(t): t for t in rule_titles}

    # initialise empty lists for every unique title
    clause_map: Dict[str, List[TextInfo]] = {title: [] for title in rule_titles}
    current_clause: Optional[str] = None

    for para in textinformation:
        para_norm = _normalize(para.text)
        matched_title: Optional[str] = None

        for norm_title, original_title in normalized_titles.items():
            if para_norm == norm_title or para_norm.startswith(norm_title):
                matched_title = original_title
                break

        if matched_title is not None:
            # This paragraph opens a new (or the same) clause.
            current_clause = matched_title
            clause_map[current_clause].append(para)
        elif current_clause is not None:
            # Body paragraph — belongs to the active clause.
            clause_map[current_clause].append(para)
        # else: before any recognised heading — skip.

    for title, paras in clause_map.items():
        if not paras:
            logger.warning("no paragraphs found for rule rule will be skipped.", rule_title=title, session_id=session_id)

    return clause_map


def _build_reviewed_rules_summary(reviewed: Dict[Tuple[str, str], PlayBookReviewResponse]) -> str:
    """Builds a concise summary of reviewed rules and their statuses for the missing clauses evaluation."""

    lines = []
    for (title, rule_type), review in reviewed.items():
        para_ids = ", ".join(review.content.para_identifiers) or "none"
        lines.append(f"RULE: {title} ({rule_type}) | STATUS: {review.content.status} | PARAS: {para_ids}")
    return "\n".join(lines) if lines else "None"


async def get_missing_clauses(llm_model: BaseLLMModel, full_text: str, reviewed_rules_summary: str, session_id: str) -> MissingClausesLLMResponse:
    """Gets missing clauses from the LLM based on the full document text and a summary of reviewed rules."""

    try:
        response: MissingClausesLLMResponse = await llm_model.generate(
            prompt=MISSING_CLAUSES_PROMPT,
            context={
                "data": full_text,
                "reviewed_rules_summary": reviewed_rules_summary,
            },
            response_model=MissingClausesLLMResponse,
            session_id=session_id,
        )
        logger.info("Missing clauses identified", count=len(response.missing_clauses), session_id=session_id)
        return response

    except Exception as exc:
        logger.exception("Missing clauses evaluation failed.", session_id=session_id)
        return MissingClausesLLMResponse(
            missing_clauses=[],
            total_missing=0,
            summary=f"LLM error: {exc}",
        )


async def _process_rule(rule: RuleInfo, clause_map: Dict[str, List[TextInfo]], llm_model: BaseLLMModel, session_id: str) -> Tuple[Tuple[str, str], PlayBookReviewResponse]:
    """Evaluates a single rule against its extracted clause paragraphs."""

    current_rule_type = getattr(rule, "rule_type", None) or getattr(rule, "type", None) or "primary"

    matched_paras: List[TextInfo] = clause_map.get(rule.title, [])

    if not matched_paras:
        logger.warning("No clause paragraphs found for rule returning empty result.", rule=rule.title, current_rule_type=current_rule_type, session_id=session_id)
        llm_response = PlayBookReviewLLMResponse(
            para_identifiers=[],
            matched_clause_name="",
            status="Not Found",
            reason="",
            suggestion="",
            suggested_fix="",
        )
        return (rule.title, current_rule_type), PlayBookReviewResponse(
            rule_type=current_rule_type,
            rule_title=rule.title,
            rule_instruction=rule.instruction,
            rule_description=rule.description,
            content=llm_response,
        )

    paragraph_context = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {html.unescape(p.text).strip()}" for p in matched_paras)

    result = RuleResult(
        title=rule.title,
        instruction=rule.instruction,
        description=rule.description,
        paragraphidentifier=",".join(p.paraindetifier for p in matched_paras),
        paragraphcontext=paragraph_context,
        similarity_scores=[],
    )

    try:
        generated_response: PlayBookReviewLLMResponse = await llm_model.generate(
            prompt=SIMILARITY_USER_PROMPT,
            context={
                "rule_title": result.title,
                "rule_instruction": result.instruction,
                "rule_description": result.description,
                "paragraphs": result.paragraphcontext,
                "rule_type": current_rule_type,
            },
            response_model=PlayBookReviewLLMResponse,
            session_id=session_id,
            system_message=SIMILARITY_SYSTEM_PROMPT,
        )
        llm_response = generated_response

    except Exception:
        logger.exception("LLM rule evaluation failed for rule", rule.title, session_id=session_id)
        llm_response = PlayBookReviewLLMResponse(
            para_identifiers=[],
            matched_clause_name="",
            status="Not Found",
            reason="",
            suggestion="",
            suggested_fix="",
        )

    return (rule.title, current_rule_type), PlayBookReviewResponse(
        rule_type=current_rule_type,
        rule_title=rule.title,
        rule_instruction=rule.instruction,
        rule_description=rule.description,
        content=llm_response,
    )


async def review_document(session_id: str, request: RuleCheckRequest, force_update_rules: Optional[List[str]] = None) -> PlayBookReviewFinalResponse:
    """Main entry point for playbook review. Extracts clauses, evaluates rules, and identifies missing clauses."""

    force_update_rules = force_update_rules or []

    llm_model = get_bedrock_model()

    rules_to_update: List[RuleInfo] = request.rulesinformation

    # Deduplicate titles for extraction — preserving order, ignoring rule_type.
    # All variants sharing a title will read from the same clause_map entry.
    unique_titles: List[str] = list(dict.fromkeys(rule.title for rule in rules_to_update))

    clause_map = extract_clauses_from_paragraphs(
        request.textinformation,
        unique_titles,
        session_id=session_id,
    )

    matched_count = sum(1 for paras in clause_map.values() if paras)
    logger.info("Clause extraction complete.", matched_count=matched_count, total_titles=len(unique_titles), session_id=session_id)

    # Evaluate all rules (including fallback variants) concurrently.
    updates: List[Tuple[Tuple[str, str], PlayBookReviewResponse]] = await asyncio.gather(
        *[
            _process_rule(
                rule=rule,
                clause_map=clause_map,
                llm_model=llm_model,
                session_id=session_id,
            )
            for rule in rules_to_update
        ]
    )

    all_reviews: List[PlayBookReviewResponse] = [result for _, result in updates]

    logger.info("Completed evaluation of %d rules.", len(all_reviews))

    full_text = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text}" for p in request.textinformation)
    reviewed_rules_summary = _build_reviewed_rules_summary(dict(updates))
    missing_clauses = await get_missing_clauses(llm_model, full_text, reviewed_rules_summary, session_id)

    return PlayBookReviewFinalResponse(
        rules_review=all_reviews,
        missing_clauses=missing_clauses,
    )
