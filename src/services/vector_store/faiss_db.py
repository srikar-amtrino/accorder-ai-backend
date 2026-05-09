import time
from typing import Any, Dict, List

import faiss
import numpy as np

from src.config.logging import Logger
from src.exceptions.faiss_exceptions import (
    FAISSDimensionMismatchException,
    FAISSEmptyEmbeddingException,
    FAISSEmptyQueryException,
    FAISSUnableToIndexException,
    FAISSUnableToSearchException,
)
from src.services.vector_store.base_store import BaseVectorStore


class FAISSVectorStore(BaseVectorStore, Logger):
    """FAISS In-Memory vector store for single vectors."""

    def __init__(self, embedding_dimension: int) -> None:
        super().__init__()
        self.dimension = embedding_dimension
        self.index = faiss.IndexFlatIP(self.dimension)

        self.stats: Dict[str, Any] = {
            "vectors_added": 0,
            "search_requests": 0,
            "total_add_time": 0.0,
            "total_search_time": 0.0,
        }

    def _validate_vectors(self, vector: np.ndarray) -> np.ndarray:
        """Ensure the vector has the correct shape and normalize for cosine similarity."""
        if vector.ndim == 1:
            vector = vector.reshape(1, -1)
        if vector.shape[1] != self.dimension:
            raise FAISSDimensionMismatchException(expected_dim=self.dimension, actual_dim=vector.shape[1])
        faiss.normalize_L2(vector)
        return vector

    async def index_embedding(self, embedding: List[float]) -> None:
        """Add a single embedding vector to the FAISS index."""
        if not embedding:
            raise FAISSEmptyEmbeddingException("Cannot index an empty embedding vector.")

        vector = np.array(embedding, dtype=np.float32)

        try:
            start_time = time.time()
            vector = self._validate_vectors(vector)
            self.index.add(vector)
            elapsed_time = time.time() - start_time
            self.logger.info(f"Indexed embedding of shape {vector.shape} into FAISS in {elapsed_time:.4f}s.")

            # Update stats
            self.stats["vectors_added"] += vector.shape[0]
            self.stats["total_add_time"] += elapsed_time

            self.logger.info(f"Added {vector.shape[0]} vector in {elapsed_time:.4f}s")
        except Exception as e:
            raise FAISSUnableToIndexException("Unable to index embeddings into database.") from e

    async def search_index(self, query_embedding: List[float], top_k: int = 5) -> Dict[str, Any]:
        """Perform cosine similarity and return the top-k indices."""

        if not query_embedding:
            raise FAISSEmptyQueryException("Query Embedding cannot be empty.")

        try:
            query = np.array(query_embedding, dtype=np.float32)

            start_time = time.time()
            query = self._validate_vectors(query)

            scores, indices = self.index.search(query, top_k)
            elapsed_time = time.time() - start_time

            self.logger.info(f"Search completed in {elapsed_time:.4f}s with top_k={top_k}.")

            return {
                "scores": scores[0].tolist(),
                "indices": indices[0].tolist(),
                "search_time": elapsed_time,
            }
        except Exception as e:
            raise FAISSUnableToSearchException("Unable to search the query in the database.") from e
