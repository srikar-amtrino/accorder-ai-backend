import time
from io import BytesIO
from typing import Any, Dict, Union

from docx import Document

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.exceptions.ingestion_exceptions import ParserNotFound
from src.schemas.registry import ParseResult
from src.services.registry.base_parser import BaseParser
from src.services.registry.registry import ParserRegistry
from src.services.session_manager import SessionData
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)
from src.services.vector_store.manager import (
    index_chunks,
    index_chunks_in_session,
)

logger = get_logger(__name__)


class IngestionService:
    """Ingestion service for processing data."""

    def __init__(self) -> None:
        """Initialize the ingestion service."""

        super().__init__()
        self.settings = get_settings()
        self.registry = ParserRegistry()
        self.vector_store = None
        from src.core.container import get_embedding_service

        self.embedding_service: BaseEmbeddingService = get_embedding_service()

    async def _parse_data(self, data: Union[BytesIO, Dict[str, Any]], session_data: SessionData = None) -> ParseResult:
        """Parse data using the registry services."""

        parser: Union[BaseParser, None] = self.registry.get_parser()

        if not parser:
            logger.error("No parser found for the given extension. Check the available parsers in the '/parsers' API.")
            raise ParserNotFound("No parser found for the given extension. Check the available parsers in the '/parsers' API.")

        if isinstance(data, BytesIO):
            start_time = time.time()
            document = Document(data)
            parsed_data: ParseResult = await parser.parse_document(document=document, session_data=session_data)
            parsed_data.processing_time = time.time() - start_time
            logger.info("Data parsed", processing_time=parsed_data.processing_time, session_id=session_data.session_id)
        else:
            start_time = time.time()
            parsed_data: ParseResult = await parser.parse_data(data=data, session_data=session_data)
            parsed_data.processing_time = time.time() - start_time
            logger.info("Data parsed", processing_time=parsed_data.processing_time, session_id=session_data.session_id)

        # Register chunks in the chunk store (the parser already populated the vector store).
        if parsed_data.chunks:
            if session_data:
                index_chunks_in_session(session_data, parsed_data.chunks, parsed_data.metadata)
                logger.info("Indexed chunks into session store.", num_chunks=len(parsed_data.chunks), session_id=session_data.session_id)
            else:
                index_chunks(parsed_data.chunks)
                logger.info("Indexed chunks into the global chunk store.", num_chunks=len(parsed_data.chunks))

        return parsed_data

    async def _get_health_status(self) -> Dict[str, Any]:
        """Get the health status of the ingestion service."""

        parser = self.registry.get_parser()
        health_info: Dict[str, Any] = {
            "parser_accessible": await parser.is_healthy() if parser else False,
            "vector_store_accessible": self.vector_store is not None,
        }

        health_info["status"] = health_info["parser_accessible"] and health_info["vector_store_accessible"]

        return health_info
