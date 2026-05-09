from abc import ABC, abstractmethod
from typing import Any, Optional

from docx.document import Document

from src.config.logging import Logger
from src.schemas.registry import ParseResult
from src.services.session_manager import SessionData


class BaseParser(ABC, Logger):
    """Abstract base class for parsers in the registry service."""

    @abstractmethod
    async def parse_document(self, document: Document, session_data: Optional["SessionData"] = None) -> ParseResult:
        """Parse the given document."""
        pass

    @abstractmethod
    async def parse_data(self, data: Any, session_data: Optional["SessionData"] = None) -> ParseResult:
        """Parse the given data."""
        pass

    @abstractmethod
    async def clean_document(self, document: Document) -> None:
        """Clean the document before parsing."""
        pass

    @abstractmethod
    def is_healthy(self) -> Any:
        """Get the health status of the parser."""
        pass
