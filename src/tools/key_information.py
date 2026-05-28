from pathlib import Path
from typing import Any

from src.dependencies import get_service_container
from src.schemas.contract_analyzer import ContractAnalyzerResponse

AGENT_NAME = "Contract Analyzer"

# Prompt split into a static system block (rules, examples, output schema) and
# a small dynamic user block (just the contract text). Loaded at import so
# per-call file I/O drops to zero.
#
# Caching (Sonnet 4.6, 1,024-token minimum): the ~2,650-token system block
# clears the minimum, but this agent runs at most once per request (and is
# session-cached on top), so there is no in-request reuse to amortize a cache
# write against. cache_system is left off; it would only pay off under
# sustained cross-request traffic hitting the same block within the ~5-min TTL.
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
