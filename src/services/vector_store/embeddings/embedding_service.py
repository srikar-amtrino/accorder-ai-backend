import time
from typing import Any, Dict, List, Optional

from sentence_transformers import SentenceTransformer

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)

logger = get_logger(__name__)


class HuggingFaceEmbeddingService(BaseEmbeddingService):
    """Hugging Face Embedding service."""

    def __init__(self) -> None:
        """Initiliaze the Service."""
        super().__init__()

        self.settings = get_settings()
        self.model_name = self.settings.huggingface_minilm_embedding_model

        self.tokenizer = SentenceTransformer(model_name_or_path=self.model_name)
        logger.info("Initialized HuggingFace Model", model_name=self.model_name)

        self.stats: Dict[str, Any] = {
            "embeddings_generated": 0,
            "total_tokens_processed": 0,
            "average_emmbedding_time": 0.0,
            "errors": 0,
            "api_calls": 0,
        }

    def get_embedding_dimensions(self) -> Any:
        """Returns the embedding dimentions."""
        return self.tokenizer.get_sentence_embedding_dimension()

    async def generate_embeddings(self, text: str, session_id: str, task: Optional[str] = None) -> List[float]:
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

            logger.debug("Generated the embeddings", session_id=session_id, text_length=len(text), embedding_length=len(embedding), generation_time=generation_time, task=task)

            return embedding

        except Exception as e:
            self.stats["errors"] += 1
            logger.error("Failed to generate embeddings", session_id=session_id, error=str(e))
            raise ValueError("Failed to generate embeddings")

    def get_stats(self) -> Dict[str, Any]:
        """Returns the statistics of the embedding service."""
        return self.stats.copy()

    async def get_health_status(self, session_id: str) -> Dict[str, Any]:
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
            test_embedding = await self.generate_embeddings(text=test_text, session_id=session_id)
            response_time = time.time() - start_time

            health_stats.update(
                {
                    "test_successfull": True,
                    "response_time": response_time,
                    "embedding_dimention": len(test_embedding),
                }
            )
            return health_stats
        except Exception as e:
            health_stats.update(
                {
                    "healthy": False,
                    "test_successful": False,
                    "errors": [str(e)],
                }
            )
            return health_stats
