import json
import time
from typing import Any, Dict, List, Optional

import requests

from src.config.logging import Logger
from src.config.settings import get_settings
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)


class JinaEmbeddings(BaseEmbeddingService, Logger):
    """Jina Embeddings from Hugging Face."""

    def __init__(self) -> None:
        """Initialize the service."""
        super().__init__()

        self.settings = get_settings()
        self.model_name = self.settings.huggingface_jina_embedding_model
        self.headers: Dict[str, Any] = {"Content-Type": "application/json", "Authorization": self.settings.jina_embedding_API}

    def get_embedding_dimensions(self) -> int:
        """Returns the embedding dimentions."""
        return 1024

    async def generate_embeddings(self, text: str, task: Optional[str] = "text-matching") -> List[float]:
        """Generate embeddings for the given text."""

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        try:
            start_time = time.time()

            # Generate Embeddings
            data: Dict[str, Any] = {
                "model": self.model_name,
                "task": task,
                "input": [text],
            }
            # print(data)
            response = requests.post(url=self.settings.jina_embedding_model_uri, headers=self.headers, data=json.dumps(data))
            data = response.json()
            # print(data)
            generation_time = time.time() - start_time
            embedding: List[float] = data["data"][0]["embedding"]

            self.logger.debug(f"Generated the embeddings in {generation_time} seconds.")

            return embedding
        except Exception as e:
            raise ValueError("Failed to embedd.") from e
