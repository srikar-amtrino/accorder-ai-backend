import time
from typing import Any, Dict, List

from openai import OpenAI
from openai.types.create_embedding_response import CreateEmbeddingResponse

from src.config.logging import Logger
from src.config.settings import get_settings


class OpenAIEmbeddings(Logger):
    """OpenAI Embedding service."""

    def __init__(self) -> None:
        """Initialize the service."""
        self.settings = get_settings()
        self.api_key = self.settings.openai_api_key
        self.model_name = self.settings.openai_embedding_model

        self.stats: Dict[str, Any] = {
            "embeddings_generated": 0,
            "total_tokens_processed": 0,
            "average_embeddings_time": 0.0,
            "erros": 0,
            "api_calls": 0,
        }

        if self.api_key is not None:
            self.client = OpenAI(api_key=self.api_key)
        else:
            raise ValueError("Unable to configure the OPENAI API KEY.")

    async def generate_embeddings(self, text: str) -> List[float]:
        """Generate embeddings using OPENAI (text-embedding-large)."""

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # Generate embeddings
            embedding: CreateEmbeddingResponse = self.client.embeddings.create(
                input=text,
                model=self.model_name,
            )
            generation_time = time.time() - start_time

            # Update the stats
            self.stats["embeddings_generated"] += 1
            self.stats["api_calls"] += 1
            self.stats["total_tokens_processed"] += len(text.split())

            self.logger.debug(f"Generated the embeddings in {generation_time} seconds.")

            return embedding.data[0].embedding

        except Exception as e:
            raise ValueError("Can't complete the emmbedding process.") from e

    def get_embedding_dimensions(self) -> int:
        """Returns the embedding dimentions."""

        if self.model_name == "text-embedding-3-small":
            return 1536
        else:
            return 3072

    def get_stats(self) -> Dict[str, Any]:
        """Returns the statistics of the embedding service."""
        return self.stats.copy()

    async def get_health_status(self) -> Dict[str, Any]:
        """Perform health check and return the status."""

        health_stats: Dict[str, Any] = {
            "healthy": True,
            "service": "OPENAIEmbeddings",
            "model": self.model_name,
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

        return health_stats
