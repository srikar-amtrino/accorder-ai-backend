import asyncio
from pathlib import Path
from typing import Any, List

from src.config.logging import get_logger
from src.dependencies import get_service_container
from src.schemas.contract_analyzer import (
    ContractAnalyzerResponse,
    MilestonesResponse,
    RisksResponse,
    SummaryKeyInfoResponse,
)

logger = get_logger("ContractAnalyzer")

AGENT_NAME = "Contract Analyzer"

# The analysis is generated as FOUR independent sections run in PARALLEL (one
# Bedrock call each): summary+key-info, milestones, present-clause risks, and
# missing clauses. Each section is a fraction of the total output and the event
# loop is no longer blocked, so the wall-clock is the slowest single section
# (~half the time of one monolithic call) while the merged result keeps the
# ContractAnalyzerResponse shape. The risks split (present vs missing) is a clean
# partition — a risk is either about an existing clause or an absent one — so the
# two lists concatenate with no overlap.
#
# `cache_system=True` lets Bedrock reuse each static section prompt across repeated
# analyses within the cache TTL.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_KEY_INFO_USER = (_PROMPTS_DIR / "key_information_user.mustache").read_text(encoding="utf-8")
_SUMMARY_SYSTEM = (_PROMPTS_DIR / "key_information_summary_system.mustache").read_text(encoding="utf-8")
_MILESTONES_SYSTEM = (_PROMPTS_DIR / "key_information_milestones_system.mustache").read_text(encoding="utf-8")
_PRESENT_RISKS_SYSTEM = (_PROMPTS_DIR / "key_information_present_risks_system.mustache").read_text(encoding="utf-8")
_MISSING_CLAUSES_SYSTEM = (_PROMPTS_DIR / "key_information_missing_clauses_system.mustache").read_text(encoding="utf-8")


async def get_key_information_document(content: str, session_id: str) -> Any:
    """Extract structured key contract details via four parallel section calls."""

    container = get_service_container()
    llm_model = container.llm_model

    session_data = container.session_manager.get_session(session_id) if session_id else None
    if not session_data:
        return ""

    agent_cache = session_data.tool_results.get(AGENT_NAME, {})
    if agent_cache:
        return agent_cache

    context = {"contract_text": content}

    def _section(system_message: str, response_model: Any, max_tokens: int) -> Any:
        return llm_model.generate(
            prompt=_KEY_INFO_USER,
            context=context,
            response_model=response_model,
            mode="JSON",
            system_message=system_message,
            cache_system=True,
            max_tokens=max_tokens,
        )

    # Run all four sections concurrently. return_exceptions=True so a single failed
    # section degrades to empty rather than failing the whole analysis.
    summary_part, milestones_part, present_risks, missing_clauses = await asyncio.gather(
        _section(_SUMMARY_SYSTEM, SummaryKeyInfoResponse, 1536),
        _section(_MILESTONES_SYSTEM, MilestonesResponse, 1536),
        _section(_PRESENT_RISKS_SYSTEM, RisksResponse, 2560),
        _section(_MISSING_CLAUSES_SYSTEM, RisksResponse, 1536),
        return_exceptions=True,
    )

    def _ok(part: Any, label: str) -> Any:
        if isinstance(part, Exception):
            logger.error(f"Contract Analyzer section '{label}' failed: {part}")
            return None
        return part

    summary_part = _ok(summary_part, "summary+key_info")
    milestones_part = _ok(milestones_part, "milestones")
    present_risks = _ok(present_risks, "present_risks")
    missing_clauses = _ok(missing_clauses, "missing_clauses")

    # Present-clause risks + missing-clause risks concatenate (clean partition, no overlap).
    risks: List[Any] = []
    if present_risks is not None:
        risks.extend(present_risks.risk_and_compliance_insights)
    if missing_clauses is not None:
        risks.extend(missing_clauses.risk_and_compliance_insights)

    response = ContractAnalyzerResponse(
        summary=summary_part.summary if summary_part is not None else "",
        key_information=summary_part.key_information if summary_part is not None else [],
        timeline_and_key_milestones=(milestones_part.timeline_and_key_milestones if milestones_part is not None else []),
        risk_and_compliance_insights=risks,
    )

    session_data.tool_results[AGENT_NAME] = response

    return response
