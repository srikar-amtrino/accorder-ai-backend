from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from src.core.auth import verify_token
from src.schemas.vector_db import ClauseSimilarityRequest
from src.services.vector_store.embeddings.embedding_client import (
    EmbeddingError,
    embedding_client,
)
from src.services.vector_store.qdrant_db import (
    QDRANT_COLLECTION,
    QdrantError,
    search_points,
)

router = APIRouter(
    tags=["Vector database"],
)


@router.post("/search")
async def search_vector_points(request: ClauseSimilarityRequest) -> dict[str, Any]:
    """Find Qdrant records relevant to the submitted clause text."""

    try:
        vectors = await embedding_client.embed([request.clause_text])
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not generate a clause embedding: {exc}",
        ) from exc

    vector = vectors[0]
    if not vector:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The embedding service returned an empty clause embedding.",
        )

    try:
        matches = search_points(
            vector=vector,
            limit=request.limit,
            score_threshold=request.score_threshold,
        )
    except QdrantError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Vector database similarity search failed: {exc}",
        ) from exc

    return {
        "collection": QDRANT_COLLECTION,
        "matches": matches,
        "num_results": len(matches),
    }
