# from pathlib import Path
# from typing import Any, Dict

# from src.core.container import (
#     get_bedrock_model,
#     get_retrieval_service,
#     get_session_manager,
# )
# from src.schemas.doc_chat import DocChatResponse

# _PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
# _DOC_CHAT_SYSTEM = (_PROMPTS_DIR / "doc_chat_system.mustache").read_text(encoding="utf-8")
# _DOC_CHAT_USER = (_PROMPTS_DIR / "doc_chat_user.mustache").read_text(encoding="utf-8")


# async def query_document(query: str, session_id: str) -> DocChatResponse:
#     """Query the document chunks based on the given query and session ID."""

#     # Get service container and session manager
#     llm_model = get_bedrock_model()
#     retrieval_service = get_retrieval_service()

#     # Retrieve relevant chunks based on query and session context
#     result = await retrieval_service.retrieve_data(
#         query=query,
#         top_k=5,
#         dynamic_k=True,
#         session_id=session_id,
#     )

#     data: Dict[str, Any] = {
#         "context": result["chunks"],
#         "question": query,
#     }

#     llm_result: DocChatResponse = await llm_model.generate(
#         prompt=_DOC_CHAT_USER,
#         context=data,
#         response_model=DocChatResponse,
#         system_message=_DOC_CHAT_SYSTEM,
#         session_id=session_id,
#     )
#     return llm_result


import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from src.config.logging import get_logger
from src.core.container import (
    get_bedrock_model,
    get_embedding_service,
)
from src.schemas.doc_chat import DocChatResponse, DocuChatRequest

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "doc_chat"
_DOC_CHAT_SYSTEM = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_DOC_CHAT_USER = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")


async def document_chat_service(session_id: str, payload: DocuChatRequest) -> DocChatResponse:
    """Service function to handle document chat queries."""

    llm_model = get_bedrock_model()
    embedding_model = get_embedding_service()

    # Generate embeddings for document chunks
    chunk_embeddings = await asyncio.gather(
        *[
            embedding_model.generate_embeddings(
                text=chunk.text,
                session_id=session_id,
            )
            for chunk in payload.textinformation
        ]
    )

    # Generate query embedding
    query_embedding = await embedding_model.generate_embeddings(
        text=payload.query,
        session_id=session_id,
    )

    # Calculate similarity scores
    similarities = cosine_similarity([query_embedding], chunk_embeddings)[0]

    # Get top 5 most relevant chunks
    top_k = 5
    top_indices = np.argsort(similarities)[::-1][:top_k]

    relevant_chunks = [payload.textinformation[idx] for idx in top_indices]

    llm_result: DocChatResponse = await llm_model.generate(
        prompt=_DOC_CHAT_USER,
        context={
            "question": payload.query,
            "document_content": [chunk.model_dump() for chunk in relevant_chunks],
        },
        response_model=DocChatResponse,
        system_message=_DOC_CHAT_SYSTEM,
        session_id=session_id,
    )

    return llm_result


async def document_chat_stream_service(session_id: str, payload: DocuChatRequest) -> Any:
    """Service function to handle streaming document chat queries."""

    try:
        llm_model = get_bedrock_model()
        embedding_model = get_embedding_service()

        # Generate embeddings for document chunks
        chunk_embeddings = await asyncio.gather(
            *[
                embedding_model.generate_embeddings(
                    text=chunk.text,
                    session_id=session_id,
                )
                for chunk in payload.textinformation
            ]
        )

        # Generate query embedding
        query_embedding = await embedding_model.generate_embeddings(
            text=payload.query,
            session_id=session_id,
        )

        # Calculate similarity scores
        similarities = cosine_similarity([query_embedding], chunk_embeddings)[0]

        # Get top 5 most relevant chunks
        top_k = 5
        top_indices = np.argsort(similarities)[::-1][:top_k]

        relevant_chunks = [payload.textinformation[idx] for idx in top_indices]

        stream = llm_model.generate_stream(
            prompt=_DOC_CHAT_USER,
            context={
                "question": payload.query,
                "document_content": [chunk.model_dump() for chunk in relevant_chunks],
            },
            session_id=session_id,
            system_message=_DOC_CHAT_SYSTEM,
        )

        async for chunk in stream:
            yield f"data: {json.dumps(chunk)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.exception(
            "Playbook review streaming failed.",
            session_id=session_id,
        )

        yield f'data: {json.dumps({"error": str(exc)})}\n\n'
