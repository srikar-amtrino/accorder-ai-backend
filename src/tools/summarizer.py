from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.dependencies import get_service_container
from src.schemas.tool_schema import SummaryToolResponse
from src.services.llm.azure_openai_model import AzureOpenAIModel
from src.services.vector_store.manager import get_all_chunks

llm_service = AzureOpenAIModel()


async def get_summary(session_id: Optional[str], response: str = "JSON") -> str | BaseModel:
    """Summary tool for the orchestrator agent or API."""

    container = get_service_container()
    session = None
    if session_id:
        try:
            session = container.session_manager.get_session(session_id)
        except Exception:
            session = None

        if not session:
            raise ValueError(f"Session '{session_id}' not found or expired")

    # Check if summary already exists in session
    if session and "summary" in session.tool_results:
        return session.tool_results["summary"]

    # Prefer session-specific chunks when session_id provided
    if session:
        results = session.chunk_store
    else:
        results = get_all_chunks()

    full_text = "\n\n".join((chunk.content for chunk in results.values() if getattr(chunk, "content", None)))

    prompt_template = Path(r"src\services\prompts\v1\summary_prompt_template.mustache").read_text()
    context = {"text": full_text}

    summary: str | SummaryToolResponse = await llm_service.generate(prompt=prompt_template, context=context, response_model=None, mode="markdown")

    # Store the result in session if session exists
    if session:
        session.tool_results["summary"] = summary

    return summary
