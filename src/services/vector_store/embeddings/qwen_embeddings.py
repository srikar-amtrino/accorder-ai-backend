import time
from typing import Any, Dict, List

from sentence_transformers import SentenceTransformer

from src.config.logging import Logger
from src.config.settings import get_settings


class Qwen3EmbeddingService(Logger):
    """Hugging Face Embedding service."""

    def __init__(self) -> None:
        """Initiliaze the Service."""

        self.settings = get_settings()
        self.model_name = self.settings.hugggingface_qwen_embedding_model

        self.tokenizer = SentenceTransformer(model_name_or_path=self.model_name)

        self.stats: Dict[str, Any] = {
            "embeddings_generated": 0,
            "total_tokens_processed": 0,
            "average_emmbedding_time": 0.0,
            "errors": 0,
            "api_calls": 0,
        }

    async def generate_embeddings(self, text: str) -> List[float]:
        """Generate embeddings for the given text."""

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # Generate embeddings
            embedding: List[float] = self.tokenizer.encode(text).tolist()
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
