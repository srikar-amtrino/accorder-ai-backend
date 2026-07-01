import json
from pathlib import Path
from typing import Any

from src.core.container import get_bedrock_model
from src.schemas.contract_analyzer import ContractAnalyzerResponse

# AGENT_NAME = "Contract Analyzer"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3" / "contract_analyzer"
_KEY_INFO_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_KEY_INFO_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")


llm_model = get_bedrock_model()


def get_key_information_stream(content: str, session_id: str) -> Any:
    """Extract structured key contract details from the given document content, streaming results as they arrive."""

    async def event_stream() -> Any:
        async for chunk in llm_model.generate_stream(prompt=_KEY_INFO_USER, context={"contract_text": content}, session_id=session_id, temperature=0.0, system_message=_KEY_INFO_SYSTEM):
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return event_stream()


async def get_key_information_generate(content: str, session_id: str) -> ContractAnalyzerResponse:
    """Extract structured key contract details from the given document content."""

    response: ContractAnalyzerResponse = await llm_model.generate(
        prompt=_KEY_INFO_USER,
        context={"contract_text": content},
        response_model=ContractAnalyzerResponse,
        session_id=session_id,
        system_message=_KEY_INFO_SYSTEM,
    )  # type: ignore

    return response
