import time
from typing import Any, Dict, List

from google import genai

from src.config.logging import Logger
from src.config.settings import get_settings


class GeminiEmbeddingService(Logger):
    """Google Gemini embedding service."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()
        self.model_name = self.settings.gemini_embedding_model
        self.api_key = self.settings.gemini_api_key

        self.stats: Dict[str, Any] = {
            "embedding_generated": 0,
            "api_calls": 0,
            "total_tokens_processed": 0,
            "average_embedding_time": 0.0,
            "errors": 0,
        }

        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
        else:
            raise ValueError("Gemini API Key is not configured.")

    async def generate_embeddings(self, text: str) -> List[float]:
        """Generate embeddings for a single text."""
        if not text or not text.strip():
            raise ValueError("Text can't be empty.")

        try:
            start_time = time.time()

            # Generate embedding
            vector_data = await self.client.aio.models.embed_content(
                model=self.model_name,
                contents=text,
            )

            # Extract float list
            embeddings: List[float] = vector_data.embeddings[0].values

            generation_time = time.time() - start_time

            # Update stats
            self.stats["embedding_generated"] += 1
            self.stats["api_calls"] += 1
            self.stats["total_tokens_processed"] += len(text.split())
            self.stats["average_embedding_time"] = (self.stats["average_embedding_time"] * (self.stats["embedding_generated"] - 1) + generation_time) / self.stats["embedding_generated"]

            self.logger.debug(f"Generated Embeddings for text ({len(text)} chars) in {generation_time:.2f}s")

            return embeddings

        except Exception as e:
            self.stats["errors"] += 1
            error_msg = f"Failed to generate embedding: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

    async def get_stats(self) -> Dict[str, Any]:
        """Return embedding services statistics."""

        return self.stats.copy()

    async def get_health_status(self) -> Dict[str, Any]:
        """Perform health check and return status."""

        health_status = {
            "healthy": True,
            "service": "GeminiEmbeddingService",
            "model": self.model_name,
            "errors": [],
            "stats": self.get_stats(),
        }

        try:
            # Test embedding generation
            test_text = "Health check test"
            start_time = time.time()

            test_embedding = await self.generate_embeddings(test_text)

            response_time = time.time() - start_time

            health_status.update(
                {
                    "test_successful": True,
                    "response_time": response_time,
                    "embedding_dimension": len(test_embedding),
                }
            )

        except Exception as e:
            health_status.update(
                {
                    "healthy": False,
                    "test_successful": False,
                    "errors": [str(e)],
                }
            )

        return health_status
