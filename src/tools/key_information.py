from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from src.dependencies import get_service_container
from src.schemas.contract_analyzer import ContractAnalyzerResponse
from src.schemas.tool_schema import KeyInformationToolResponse
from src.services.llm.bedrock_model import BedrockModel
from src.services.vector_store.manager import get_all_chunks

_llm = BedrockModel()

AGENT_NAME = "Contract Analyzer"

# Prompt split into a static system block (rules, examples, output schema) and
# a small dynamic user block (just the contract text). Loaded at import so
# per-call file I/O drops to zero. System block is ~1.8K tokens — below the
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


async def get_key_information(session_id: Optional[str] = None, response_format: str = "JSON") -> Any | BaseModel:
    """Extract structured key contract details from the currently ingested document."""

    container = get_service_container()
    session = None
    if session_id:
        try:
            session = container.session_manager.get_session(session_id)
        except Exception:
            session = None

        if not session:
            raise ValueError(f"Session '{session_id}' not found or expired")

    # Check if key information already exists in session
    if session and "key_information" in session.tool_results:
        return session.tool_results["key_information"]

    # Prefer session-specific chunks if session_id is provided
    if session:
        results = session.chunk_store
    else:
        results = get_all_chunks()

    if not results:
        raise ValueError("No document ingested. Please ingest a document first.")

    full_text = "\n\n".join(chunk.content for chunk in results.values() if getattr(chunk, "content", None))

    response: str | KeyInformationToolResponse = await _llm.generate(
        prompt=_KEY_INFO_USER,
        context={"contract_text": full_text},
        response_model=None,
        mode="markdown",
        system_message=_KEY_INFO_SYSTEM,
    )

    # Store the result in session if session exists
    if session:
        session.tool_results["key_information"] = response

    return response
