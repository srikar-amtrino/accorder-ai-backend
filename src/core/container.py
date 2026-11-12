from dataclasses import dataclass, field
from typing import Any, Optional

from src.config.settings import get_settings
from src.services.clause_mapping.qdrant_search import QdrantClauseSearch
from src.services.clause_mapping.search import BaseClauseSearch
from src.services.llm.bedrock_model import BedrockModel
from src.services.session_manager import SessionManager

try:
    from src.services.ingestion.ingestion import IngestionService
except (ImportError, OSError, Exception):
    IngestionService = Any  # type: ignore

try:
    from src.services.retrieval.retrieval import RetrievalService
except (ImportError, OSError, Exception):
    RetrievalService = Any  # type: ignore

try:
    from src.services.vector_store.embeddings.embedding_service import (
        HuggingFaceEmbeddingService,
    )
except (ImportError, OSError, Exception):
    HuggingFaceEmbeddingService = Any  # type: ignore

try:
    from src.services.vector_store.faiss_db import FAISSVectorStore
except (ImportError, OSError, Exception):
    FAISSVectorStore = Any  # type: ignore


@dataclass
class AppContainer:
    """Dependency injection container for the application."""

    bedrock_model: Optional[BedrockModel] = field(default=None)
    session_manager: Optional[SessionManager] = field(default=None)
    embedding_service: Optional[Any] = field(default=None)
    retrieval_service: Optional[Any] = field(default=None)
    ingestion_service: Optional[Any] = field(default=None)
    faiss_store: Optional[Any] = field(default=None)
    clause_search: Optional[BaseClauseSearch] = field(default=None)

    def initialize(self) -> None:
        """Initialization of components"""

        self.bedrock_model = BedrockModel()
        self.session_manager = SessionManager()
        if HuggingFaceEmbeddingService is not Any:
            try:
                self.embedding_service = HuggingFaceEmbeddingService()
                if FAISSVectorStore is not Any:
                    try:
                        self.faiss_store = FAISSVectorStore(embedding_dimension=self.embedding_service.get_embedding_dimensions())
                    except Exception as e:
                        import logging as _logging
                        _logging.getLogger(__name__).warning("FAISSVectorStore init skipped: %s", e)
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "HuggingFaceEmbeddingService unavailable (torch/sentence-transformers blocked by OS policy). "
                    "Local embeddings disabled; Qdrant remote embedding client will be used. Error: %s", e
                )
        if RetrievalService is not Any:
            try:
                self.retrieval_service = RetrievalService()
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).warning("RetrievalService init skipped: %s", e)
        if IngestionService is not Any:
            try:
                self.ingestion_service = IngestionService()
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).warning("IngestionService init skipped: %s", e)
        self.clause_search = QdrantClauseSearch() if get_settings().clause_search_enabled else None


container = AppContainer()


def get_bedrock_model() -> BedrockModel:
    """Get the BedrockModel instance from the container."""

    if container.bedrock_model is None:
        container.bedrock_model = BedrockModel()
    return container.bedrock_model


def get_session_manager() -> SessionManager:
    """Get the SessionManager instance from the container."""

    if container.session_manager is None:
        container.session_manager = SessionManager()
    return container.session_manager


def get_embedding_service() -> Any:
    """Get the HuggingFaceEmbeddingService instance from the container."""

    if container.embedding_service is None and HuggingFaceEmbeddingService is not Any:
        try:
            container.embedding_service = HuggingFaceEmbeddingService()
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning("get_embedding_service: HuggingFaceEmbeddingService unavailable: %s", e)
    return container.embedding_service


def get_retrieval_service() -> Any:
    """Get the RetrievalService instance from the container."""

    if container.retrieval_service is None:
        raise RuntimeError("RetrievalService has not been initialized. Call container.initialize() before using it.")

    return container.retrieval_service


def get_ingestion_service() -> Any:
    """Get the IngestionService instance from the container."""

    if container.ingestion_service is None:
        raise RuntimeError("IngestionService has not been initialized. Call container.initialize() before using it.")

    return container.ingestion_service


def get_faiss_store() -> Any:
    """Get the FAISSVectorStore instance from the container."""

    if container.faiss_store is None:
        raise RuntimeError("FAISSVectorStore has not been initialized. Call container.initialize() before using it.")

    return container.faiss_store


def get_clause_search() -> Optional[BaseClauseSearch]:
    """Get the clause vector search client, or None when none is configured."""

    if container.clause_search is None and get_settings().clause_search_enabled:
        try:
            container.clause_search = QdrantClauseSearch()
        except Exception:
            pass

    return container.clause_search
