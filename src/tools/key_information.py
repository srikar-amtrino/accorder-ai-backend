from pathlib import Path
from typing import Any

from src.config.logging import get_logger
from src.dependencies import get_service_container
from src.schemas.contract_analyzer import ContractAnalyzerResponse

logger = get_logger("ContractAnalyzer")

AGENT_NAME = "Contract Analyzer"

# ONE Bedrock call. JSON mode forces tool-use against the ContractAnalyzerResponse schema, so
# Bedrock returns a schema-validated object directly — no markdown fences, no brittle json.loads,
# no NDJSON. The prompt keeps the output concise (material Critical/High items, terse values), so
# a dense contract lands around ~850 output tokens / ~17-19s on Sonnet rather than a 1.7k-token,
# ~32s exhaustive dump. Latency is output-bound (~50 tok/sec), so concision is the lever.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_KEY_INFO_USER = (_PROMPTS_DIR / "key_information_user.mustache").read_text(encoding="utf-8")
_KEY_INFO_SYSTEM = (_PROMPTS_DIR / "key_information_system.mustache").read_text(encoding="utf-8")

# Generous headroom so the schema-validated tool-use JSON is NEVER truncated mid-object (a cut-off
# tool_input fails json.loads and 500s). Tool-use JSON is token-dense — braces, quotes, colons and
# field names are each tokens, so ~1k tokens is only ~2KB of JSON. The PROMPT (not this cap) keeps
# the response small and fast; this is only a runaway guard and the model stops well before it.
_MAX_TOKENS = 2048

# Section names the UI paints, in order.
_SECTIONS = ["summary", "key_information", "timeline_and_key_milestones", "risk_and_compliance_insights"]


def _empty_response() -> ContractAnalyzerResponse:
    return ContractAnalyzerResponse(
        summary="",
        key_information=[],
        timeline_and_key_milestones=[],
        risk_and_compliance_insights=[],
    )


async def _analyze(content: str) -> ContractAnalyzerResponse:
    """Run the single tool-use call and return a validated ContractAnalyzerResponse."""

    container = get_service_container()
    return await container.llm_model.generate(
        prompt=_KEY_INFO_USER,
        context={"contract_text": content},
        response_model=ContractAnalyzerResponse,
        mode="JSON",
        system_message=_KEY_INFO_SYSTEM,
        cache_system=True,
        max_tokens=_MAX_TOKENS,
    )


async def get_key_information_document(content: str, session_id: str) -> Any:
    """Analyze a contract in a single call and return the structured result (session-cached)."""

    container = get_service_container()
    session_data = container.session_manager.get_session(session_id) if session_id else None
    if not session_data:
        return ""

    cached = session_data.tool_results.get(AGENT_NAME)
    if cached:
        return cached

    response = await _analyze(content)
    session_data.tool_results[AGENT_NAME] = response
    return response


async def stream_key_information(content: str, session_id: str) -> Any:
    """Emit the analysis to the SSE endpoint as discrete section events.

    The analysis is one JSON call (not token-streamed); once it lands, the result is replayed
    section-by-section so the existing client/demo contract is preserved:
      ``start``   {"sections": [...]}
      ``summary`` {"text": "..."}
      ``item``    {"section": "<name>", "value": {...}}
      ``done``    {"cached": bool}
      ``error``   {"message": "..."}
    """

    container = get_service_container()
    session_data = container.session_manager.get_session(session_id) if session_id else None

    yield ("start", {"sections": _SECTIONS})

    cached = session_data.tool_results.get(AGENT_NAME) if session_data else None
    is_cached = cached is not None
    try:
        response = cached if is_cached else await _analyze(content)
    except Exception as exc:
        logger.error(f"Contract Analyzer (stream) failed: {exc}")
        yield ("error", {"message": "Analysis failed. Please retry."})
        return

    if not is_cached and session_data is not None:
        session_data.tool_results[AGENT_NAME] = response

    yield ("summary", {"text": response.summary})
    for item in response.key_information:
        yield ("item", {"section": "key_information", "value": item.model_dump()})
    for item in response.timeline_and_key_milestones:
        yield ("item", {"section": "timeline_and_key_milestones", "value": item.model_dump()})
    for item in response.risk_and_compliance_insights:
        yield ("item", {"section": "risk_and_compliance_insights", "value": item.model_dump()})
    yield ("done", {"cached": is_cached})
