from pathlib import Path
from typing import Any, Dict

from src.core.container import (
    get_bedrock_model,
    get_retrieval_service,
    get_session_manager,
)
from src.schemas.doc_chat import DocChatResponse

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_DOC_CHAT_SYSTEM = (_PROMPTS_DIR / "doc_chat_system.mustache").read_text(encoding="utf-8")
_DOC_CHAT_USER = (_PROMPTS_DIR / "doc_chat_user.mustache").read_text(encoding="utf-8")


async def query_document(query: str, session_id: str) -> DocChatResponse:
    """Query the document chunks based on the given query and session ID."""

    # Get service container and session manager
    llm_model = get_bedrock_model()
    retrieval_service = get_retrieval_service()

    # Retrieve relevant chunks based on query and session context
    result = await retrieval_service.retrieve_data(
        query=query,
        top_k=5,
        dynamic_k=True,
        session_id=session_id,
    )

    data: Dict[str, Any] = {
        "context": result["chunks"],
        "question": query,
    }

    llm_result: DocChatResponse = await llm_model.generate(
        prompt=_DOC_CHAT_USER,
        context=data,
        response_model=DocChatResponse,
        system_message=_DOC_CHAT_SYSTEM,
        session_id=session_id,
    )
    return llm_result
