import time
from io import BytesIO
from typing import Any, Dict, Union

from docx import Document

from src.config.logging import Logger
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


class IngestionService(Logger):
    """Ingestion service for processing data."""

    def __init__(self) -> None:
        """Initialize the ingestion service."""

        super().__init__()
        self.settings = get_settings()
        self.registry = ParserRegistry()
        self.vector_store = None
        from src.dependencies import get_service_container

        service_container = get_service_container()
        self.embedding_service: BaseEmbeddingService = service_container.embedding_service

    async def _parse_data(self, data: Union[BytesIO, Dict[str, Any]], session_data: SessionData = None) -> ParseResult:
        """Parse data using the registry services."""

        parser: Union[BaseParser, None] = self.registry.get_parser()

        if not parser:
            self.logger.error("No parser found for the given extension. Check the available parsers in the '/parsers' API.")
            raise ParserNotFound("No parser found for the given extension. Check the available parsers in the '/parsers' API.")

        if isinstance(data, BytesIO):
            start_time = time.time()
            document = Document(data)
            parsed_data: ParseResult = await parser.parse_document(document=document, session_data=session_data)
            parsed_data.processing_time = time.time() - start_time
            self.logger.info(f"Data parsed in {parsed_data.processing_time:.2f} seconds for the document {document}.")
        else:
            start_time = time.time()
            parsed_data: ParseResult = await parser.parse_data(data=data, session_data=session_data)
            parsed_data.processing_time = time.time() - start_time
            self.logger.info(f"Data parsed in {parsed_data.processing_time:.2f} seconds for the provided data.")

        # Index the chunks in the vector store
        if parsed_data.chunks:
            # Generate embeddings for each chunk and index them
            for chunk in parsed_data.chunks:
                # Generate embedding for the chunk content
                embedding = await self.embedding_service.generate_embeddings(text=chunk.content, task="text-matching")

                # Index embedding into the appropriate vector store
                if session_data:
                    await session_data.vector_store.index_embedding(embedding)
                else:
                    # For global indexing, use the global vector store
                    from src.services.vector_store.manager import get_faiss_vector_store

                    global_store = get_faiss_vector_store(self.embedding_service.get_embedding_dimensions())
                    await global_store.index_embedding(embedding)

            # Index chunks into chunk store
            if session_data:
                # Per-session indexing (include document metadata)
                index_chunks_in_session(session_data, parsed_data.chunks, parsed_data.metadata)
                self.logger.info(f"Indexed {len(parsed_data.chunks)} chunks into " f"session {session_data.session_id}")
            else:
                # Global indexing (legacy)
                index_chunks(parsed_data.chunks)
                self.logger.info(f"Indexed {len(parsed_data.chunks)} chunks into the vector store.")

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
