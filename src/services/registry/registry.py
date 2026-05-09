from typing import Dict, Optional

from src.config.logging import Logger
from src.exceptions.parser_exceptions import ParserAlreadyRegistered

# from src.services.registry.ai_parser import AIParser
from src.services.registry.base_parser import BaseParser

# from src.services.registry.doc_parser import DocxParser
from src.services.registry.semantic_parser import DocxParser


class ParserRegistry(Logger):
    """Registry Service for Parsers."""

    def __init__(self) -> None:
        """Initialize the ParserRegistry with an empty registry."""

        self.parsers: Dict[str, BaseParser] = {}
        self._register_default_parsers()

    def _register_default_parsers(self) -> None:
        """Register default parsers in the registry."""

        self.parsers["DOCX"] = DocxParser()
        # self.parsers["DOCX"] = AIParser()
        self.logger.info("Registered default parsers into the registry: DOCX")

    def register_parser(self, name: str, parser_class: BaseParser) -> None:
        """Register a parser class in the registry."""

        if name in self.parsers:
            raise ParserAlreadyRegistered(f"Parser '{name}' is already registered.")
        self.parsers[name] = parser_class
        self.logger.info(f"Registered a new parser '{name}' into the registry.")

    # Need to implement this method
    def get_parser(self) -> Optional[BaseParser]:
        """Retrive the relavent parser class from the registry based on file extension."""

        return self.parsers.get("DOCX")
