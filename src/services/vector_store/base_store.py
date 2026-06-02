from abc import ABC, abstractmethod
from typing import List


class BaseVectorStore(ABC):
    """Base class for Vector Database."""

    @abstractmethod
    async def index_embedding(self, embedding: List[float], session_id: str) -> None:
        """Index the given embedding into the vector database."""
        pass
