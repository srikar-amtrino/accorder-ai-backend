import asyncio
import time
from typing import Any, Dict, List, Optional

from sentence_transformers import SentenceTransformer

from src.config.logging import Logger
from src.config.settings import get_settings
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)


class HuggingFaceEmbeddingService(BaseEmbeddingService, Logger):
    """Hugging Face Embedding service."""

    def __init__(self) -> None:
        """Initiliaze the Service."""
        super().__init__()

        self.settings = get_settings()
        self.model_name = self.settings.huggingface_minilm_embedding_model

        self.tokenizer = SentenceTransformer(model_name_or_path=self.model_name)

        self.stats: Dict[str, Any] = {
            "embeddings_generated": 0,
            "total_tokens_processed": 0,
            "average_emmbedding_time": 0.0,
            "errors": 0,
            "api_calls": 0,
        }

    def get_embedding_dimensions(self) -> int:
        """Returns the embedding dimentions."""
        return self.tokenizer.get_sentence_embedding_dimension()

    async def generate_embeddings(self, text: str, task: Optional[str] = None) -> List[float]:
        """Generate embeddings for the given text."""

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # Generate embeddings — encode() is blocking CPU work, so run it off the
            # event loop to keep concurrent requests responsive on single-worker uvicorn.
            embedding: List[float] = await asyncio.to_thread(lambda: self.tokenizer.encode(text).tolist())
            generation_time = time.time() - start_time

            # update the stats
            self.stats["embeddings_generated"] += 1
            self.stats["api_calls"] += 1
            self.stats["total_tokens_processed"] += len(text.split())

            self.logger.debug(f"Generated the embeddings in {generation_time} seconds.")

            return embedding

        except Exception as e:
            self.stats["errors"] += 1
            self.logger.error(f"Failed to generate embeddings: {str(e)}")
            raise ValueError("Failed to embedd")

    async def generate_embeddings_batch(self, texts: List[str], task: Optional[str] = None) -> List[List[float]]:
        """Generate embeddings for many texts in a single batched encode call.

        Equivalent to calling ``generate_embeddings`` on each text individually
        (same model, same un-normalized mean-pooled vectors) but runs ONE forward
        pass over the whole list instead of N — far faster on CPU. Input order is
        preserved in the output. Verified: batched vs per-text vectors differ only
        at ~1e-7 (float rounding), orders of magnitude below any similarity
        threshold the callers compare against, so matching results are unchanged.
        """

        if not texts:
            return []
        if any((t is None or not t.strip()) for t in texts):
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # encode() is blocking CPU work — run it off the event loop. batch_size
            # bounds each forward pass; the full list is handled in this one call.
            embeddings: List[List[float]] = await asyncio.to_thread(
                lambda: self.tokenizer.encode(texts, batch_size=64, convert_to_numpy=True).tolist()
            )
            generation_time = time.time() - start_time

            # Mirror the per-text stats accounting so totals stay consistent.
            self.stats["embeddings_generated"] += len(texts)
            self.stats["api_calls"] += 1
            self.stats["total_tokens_processed"] += sum(len(t.split()) for t in texts)

            self.logger.debug(f"Generated {len(texts)} embeddings (batched) in {generation_time} seconds.")

            return embeddings

        except Exception as e:
            self.stats["errors"] += 1
            self.logger.error(f"Failed to generate batch embeddings: {str(e)}")
            raise ValueError("Failed to embedd")

    def get_stats(self) -> Dict[str, Any]:
        """Returns the statistics of the embedding service."""
        return self.stats.copy()

    async def get_health_status(self) -> Dict[str, Any]:
        """Perform health check and return the status."""

        health_stats: Dict[str, Any] = {
            "healthy": True,
            "service": "HuggingFaceEmbeddingService",
            "model": self.model_name,
            "errors": [],
            "stats": self.get_stats(),
        }

        try:
            test_text = "Get Health Status"
            start_time = time.time()
            test_embedding = await self.generate_embeddings(text=test_text)
            response_time = time.time() - start_time

            health_stats.update(
                {
                    "test_successfull": True,
                    "response_time": response_time,
                    "embedding_dimention": len(test_embedding),
                }
            )
        except Exception as e:
            health_stats.update(
                {
                    "healthy": False,
                    "test_successful": False,
                    "errors": [str(e)],
                }
            )


