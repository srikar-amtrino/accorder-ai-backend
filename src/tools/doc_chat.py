from pathlib import Path
from typing import Any, Dict

from src.dependencies import get_service_container
from src.schemas.doc_chat import DocChatResponse

# Prompt split into static system (rules, examples, schema) and dynamic user
# (retrieved context chunks + question).
#
# Caching (Sonnet 4.6, 1,024-token minimum): the ~2,080-token system block
# clears the minimum, but query_document runs once per question — no in-request
# reuse — so cache_system is left off. Worth enabling only if sustained traffic
# makes the same block recur within the ~5-min cache TTL.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_DOC_CHAT_SYSTEM = (_PROMPTS_DIR / "doc_chat_system.mustache").read_text(encoding="utf-8")
_DOC_CHAT_USER = (_PROMPTS_DIR / "doc_chat_user.mustache").read_text(encoding="utf-8")


async def query_document(query: str, session_id: str) -> DocChatResponse:
    """Query the document chunks based on the given query and session ID."""

    # Get service container and session manager
    service_container = get_service_container()
    session_manager = service_container.session_manager
    retrieval_service = service_container.retrieval_service
    llm_model = service_container.llm_model

    # Get session data
    session_data = session_manager.get_session(session_id)
    if not session_data:
        return {"error": "Session not found. Please ingest documents first.", "session_id": session_id}

    # Retrieve relevant chunks based on query and session context
    result = await retrieval_service.retrieve_data(
        query=query,
        top_k=5,
        dynamic_k=True,
        session_data=session_data,
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
        max_tokens=2048,  # answer list grounded in retrieved context
    )
    return llm_result
