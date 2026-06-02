from typing import Dict, Optional

from src.config.logging import get_logger
from src.exceptions.parser_exceptions import ParserAlreadyRegistered
from src.services.registry.base_parser import BaseParser
from src.services.registry.semantic_parser import DocxParser

logger = get_logger(__name__)


class ParserRegistry:
    """Registry Service for Parsers."""

    def __init__(self) -> None:
        """Initialize the ParserRegistry with an empty registry."""

        self.parsers: Dict[str, BaseParser] = {}
        self._register_default_parsers()

    def _register_default_parsers(self) -> None:
        """Register default parsers in the registry."""

        self.parsers["DOCX"] = DocxParser()
        logger.info("Registered default parsers into the registry: DOCX")

    def register_parser(self, name: str, parser_class: BaseParser) -> None:
        """Register a parser class in the registry."""

        if name in self.parsers:
            raise ParserAlreadyRegistered(f"Parser '{name}' is already registered.")
        self.parsers[name] = parser_class
        logger.info("Registered a new parser into the registry.", parser_name=name)

    # Need to implement this method
    def get_parser(self) -> Optional[BaseParser]:
        """Retrive the relavent parser class from the registry based on file extension."""

        return self.parsers.get("DOCX")
