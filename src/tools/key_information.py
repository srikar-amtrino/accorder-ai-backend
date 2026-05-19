from pathlib import Path
from typing import Any

from src.dependencies import get_service_container
from src.schemas.contract_analyzer import ContractAnalyzerResponse

AGENT_NAME = "Contract Analyzer"

# Prompt split into a static system block (rules, examples, output schema) and
# a small dynamic user block (just the contract text). Loaded at import so
# per-call file I/O drops to zero. System block is ~2.5K tokens — below the
# Opus 4.7 cache minimum, so no cache_control. Split is for adherence quality
# and architectural consistency with the other agents.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_KEY_INFO_SYSTEM = (_PROMPTS_DIR / "key_information_system.mustache").read_text(encoding="utf-8")
_KEY_INFO_USER = (_PROMPTS_DIR / "key_information_user.mustache").read_text(encoding="utf-8")


async def get_key_information_document(content: str, session_id: str) -> Any:
    """Extract structured key contract details from the given document content."""

    container = get_service_container()
    llm_model = container.llm_model

    session_data = container.session_manager.get_session(session_id) if session_id else None
    if not session_data:
        return ""

    agent_cache = session_data.tool_results.get(AGENT_NAME, {})
    if agent_cache:
        return agent_cache

    response: str = await llm_model.generate(
        prompt=_KEY_INFO_USER,
        context={"contract_text": content},
        response_model=ContractAnalyzerResponse,
        mode="JSON",
        system_message=_KEY_INFO_SYSTEM,
    )

    session_data.tool_results[AGENT_NAME] = response

    return response
