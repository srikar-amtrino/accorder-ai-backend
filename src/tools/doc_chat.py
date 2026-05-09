from pathlib import Path
from typing import Any, Dict

from src.dependencies import get_service_container
from src.schemas.doc_chat import DocChatResponse


async def query_document(query: str, session_id: str) -> DocChatResponse:
    """Query the document chunks based on the given query and session ID."""

    # Get service container and session manager
    service_container = get_service_container()
    session_manager = service_container.session_manager
    retrieval_service = service_container.retrieval_service
    azure_model = service_container.azure_openai_model

    prompt = Path(r"src\services\prompts\v1\llm_response.mustache").read_text(encoding="utf-8")

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

    llm_result: DocChatResponse = await azure_model.generate(prompt=prompt, context=data, response_model=DocChatResponse)
    return llm_result
