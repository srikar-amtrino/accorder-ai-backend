from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.dependencies import get_service_container
from src.schemas.tool_schema import SummaryToolResponse
from src.services.llm.bedrock_model import BedrockModel
from src.services.vector_store.manager import get_all_chunks

llm_service = BedrockModel()

# Prompt split into static system (rules + 1 example) and dynamic user
# (just the document text). System ~750 tokens — below cache minimum.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_SUMMARIZER_SYSTEM = (_PROMPTS_DIR / "summarizer_system.mustache").read_text(encoding="utf-8")
_SUMMARIZER_USER = (_PROMPTS_DIR / "summarizer_user.mustache").read_text(encoding="utf-8")


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

    context = {"text": full_text}

    summary: str | SummaryToolResponse = await llm_service.generate(
        prompt=_SUMMARIZER_USER,
        context=context,
        response_model=None,
        mode="markdown",
        system_message=_SUMMARIZER_SYSTEM,
    )

    # Store the result in session if session exists
    if session:
        session.tool_results["summary"] = summary

    return summary
