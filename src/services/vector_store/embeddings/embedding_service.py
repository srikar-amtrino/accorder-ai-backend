import asyncio
import time
from typing import Any, Dict, List, Optional

import torch
from sentence_transformers import SentenceTransformer
from transformers import AutoModel, AutoTokenizer

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
        self.model_name = self.settings.hugggingface_minilm_embedding_model

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


# service = HuggingFaceEmbeddingService()
# print(service.get_embedding_dimensions())


# Too expensive and consuming a lot of resources.
class BGEEmbeddingService(Logger):
    """Local BGE Embedding service."""

    def __init__(self) -> None:
        """Initialize the service with BGE model."""

        self.settings = get_settings()
        self.model_name = "BAAI/bge-large-en-v1.5"

        # Tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModel.from_pretrained(self.model_name)
        self.model.to("cpu")
        self.model.eval()

        self.stats: Dict[str, Any] = {
            "embeddings_generated": 0,
            "errors": 0,
            "api_calls": 0,
            "average_embedding_time": 0.0,
        }

    async def generate_embeddings(self, text: str, task: Optional[str] = "") -> List[float]:
        """Generate a normalized embedding for a single text."""
        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # Run model in thread pool to avoid blocking async loop
            embedding = await asyncio.to_thread(self._embed_text, text)

            generation_time = time.time() - start_time
            self.stats["embeddings_generated"] += 1
            self.stats["api_calls"] += 1
            self.stats["average_embedding_time"] = (self.stats["average_embedding_time"] + generation_time) / 2

            self.logger.debug(f"Generated embedding in {generation_time:.4f}s")
            return embedding.tolist()

        except Exception as e:
            self.stats["errors"] += 1
            self.logger.error(f"BGE embedding failed: {e}")
            raise

    def _embed_text(self, text: str) -> torch.Tensor:
        """Synchronous embedding generation."""
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=1536)
        inputs = {k: v.to("cpu") for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            # Mean pooling over sequence
            embedding = outputs.last_hidden_state.mean(dim=1)
            # Normalize for cosine similarity
            embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
        return embedding[0]

    def get_embedding_dimensions(self) -> int:
        """Returns the embedding dimentions."""
        return self.model.config.hidden_size

    def get_stats(self) -> Dict[str, Any]:
        return self.stats.copy()

    async def get_health_status(self) -> Dict[str, Any]:
        """Perform a simple embedding test."""
        try:
            embedding = await self.generate_embeddings("Health check")
            return {
                "healthy": True,
                "model": self.model_name,
                "embedding_dimension": len(embedding),
            }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
            }
