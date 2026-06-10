from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel


class BaseLLMModel(ABC):
    """Base interface for all LLM model implementations."""

    @abstractmethod
    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type[BaseModel], session_id: str, system_message: Optional[str] = None, cache_system: bool = False, max_tokens: int = 10000) -> BaseModel:
        """Generate a response from the model."""
        pass

    @abstractmethod
    async def generate_stream(self, prompt: str, context: Dict[str, Any], session_id: str, system_message: Optional[str] = None) -> Any:
        """Generate a streaming response from the model."""
        pass
