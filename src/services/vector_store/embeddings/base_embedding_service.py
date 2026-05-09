from abc import ABC, abstractmethod
from typing import List, Optional


class BaseEmbeddingService(ABC):
    """Base Class for Embedding Serivces."""

    @abstractmethod
    def get_embedding_dimensions(self) -> int:
        """Returns the embedding dimension of the model used."""
        pass

    @abstractmethod
    async def generate_embeddings(self, text: str, task: Optional[str]) -> List[float]:
        """Generate embeddings for the given text."""
        pass
