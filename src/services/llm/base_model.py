from abc import ABC, abstractmethod
from typing import Any, Dict, Type


class BaseLLMModel(ABC):
    """Base interface for all LLM model implementations."""

    @abstractmethod
    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type) -> str:
        """Generate a response from the model."""
        pass
