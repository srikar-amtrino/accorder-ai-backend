from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.dependencies import get_service_container
from src.schemas.contract_analyzer import ContractAnalyzerResponse
from src.schemas.tool_schema import KeyInformationToolResponse
from src.services.llm.azure_openai_model import AzureOpenAIModel
from src.services.vector_store.manager import get_all_chunks

_llm = AzureOpenAIModel()

AGENT_NAME = "Contract Analyzer"


async def get_key_information_document(content: str, session_id: str) -> str:
    """Extract structured key contract details from the given document content."""

    container = get_service_container()
    llm_model = container.azure_openai_model

    session_data = container.session_manager.get_session(session_id) if session_id else None
    if not session_data:
        return ""

    agent_cache = session_data.tool_results.get(AGENT_NAME, {})
    if agent_cache:
        return agent_cache

    prompt_path = Path(r"src\services\prompts\v1\key_information_prompt.mustache").read_text(encoding="utf-8")

    response: str = await llm_model.generate(
        prompt=prompt_path,
        context={"contract_text": content},
        response_model=ContractAnalyzerResponse,
        mode="JSON",
    )

    session_data.tool_results[AGENT_NAME] = response

    return response


async def get_key_information(session_id: Optional[str] = None, response_format: str = "JSON") -> str | BaseModel:
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

    prompt_path = Path(r"src\services\prompts\v1\key_information_prompt.mustache").read_text(encoding="utf-8")

    response: str | KeyInformationToolResponse = await _llm.generate(
        prompt=prompt_path,
        context={"contract_text": full_text},
        response_model=None,
        mode="markdown",
    )

    # Store the result in session if session exists
    if session:
        session.tool_results["key_information"] = response

    return response
