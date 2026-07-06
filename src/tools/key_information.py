import asyncio
import json
from typing import Any

from src.core.container import get_bedrock_model
from src.schemas.contract_analyzer import ContractAnalyzerResponse
from src.services.risk_consensus import analyze_contract_consensus

# AGENT_NAME = "Contract Analyzer"

llm_model = get_bedrock_model()

# Size of each SSE text fragment when re-streaming the consensus JSON. Kept small
# so the Word add-in still receives the answer chunk by chunk (unchanged SSE
# contract: each event is `data: "<fragment>"`, the client concatenates them).
_STREAM_CHUNK_CHARS = 48


def get_key_information_stream(content: str, session_id: str) -> Any:
    """Analyze a contract and stream the stable consensus result chunk by chunk."""

    async def event_stream() -> Any:
        result = await analyze_contract_consensus(llm_model, content, session_id)
        payload = result.model_dump_json()
        for start in range(0, len(payload), _STREAM_CHUNK_CHARS):
            yield f"data: {json.dumps(payload[start : start + _STREAM_CHUNK_CHARS])}\n\n"
            await asyncio.sleep(0)  # give control back to the event loop between chunks
        yield "data: [DONE]\n\n"

    return event_stream()


async def get_key_information_generate(content: str, session_id: str) -> ContractAnalyzerResponse:
    """Analyze a contract and return the stable consensus result."""

    return await analyze_contract_consensus(llm_model, content, session_id)
