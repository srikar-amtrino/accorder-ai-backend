from dataclasses import dataclass, field
from typing import Optional

from src.services.ingestion.ingestion import IngestionService
from src.services.llm.bedrock_model import BedrockModel
from src.services.retrieval.retrieval import RetrievalService
from src.services.session_manager import SessionManager
from src.services.vector_store.embeddings.embedding_service import (
    HuggingFaceEmbeddingService,
)
from src.services.vector_store.faiss_db import FAISSVectorStore


@dataclass
class AppContainer:
    """Dependency injection container for the application."""

    bedrock_model: BedrockModel = field(default=BedrockModel())
    session_manager: SessionManager = field(default=SessionManager())
    embedding_service: HuggingFaceEmbeddingService = field(default=HuggingFaceEmbeddingService())
    retrieval_service: Optional[RetrievalService] = field(default=None)
    ingestion_service: Optional[IngestionService] = field(default=None)
    faiss_store: Optional[FAISSVectorStore] = field(default=None)

    def initialize(self) -> None:
        """Initialization of components"""

        self.bedrock_model = BedrockModel()
        self.session_manager = SessionManager()
        self.embedding_service = HuggingFaceEmbeddingService()
        self.retrieval_service = RetrievalService()
        self.ingestion_service = IngestionService()
        self.faiss_store = FAISSVectorStore(embedding_dimension=self.embedding_service.get_embedding_dimensions())


container = AppContainer()


def get_bedrock_model() -> BedrockModel:
    """Get the BedrockModel instance from the container."""

    return container.bedrock_model


def get_session_manager() -> SessionManager:
    """Get the SessionManager instance from the container."""

    return container.session_manager


def get_embedding_service() -> HuggingFaceEmbeddingService:
    """Get the HuggingFaceEmbeddingService instance from the container."""

    return container.embedding_service


def get_retrieval_service() -> RetrievalService:
    """Get the RetrievalService instance from the container."""

    if container.retrieval_service is None:
        raise RuntimeError("RetrievalService has not been initialized. Call container.initialize() before using it.")

    return container.retrieval_service


def get_ingestion_service() -> IngestionService:
    """Get the IngestionService instance from the container."""

    if container.ingestion_service is None:
        raise RuntimeError("IngestionService has not been initialized. Call container.initialize() before using it.")

    return container.ingestion_service


def get_faiss_store() -> FAISSVectorStore:
    """Get the FAISSVectorStore instance from the container."""

    if container.faiss_store is None:
        raise RuntimeError("FAISSVectorStore has not been initialized. Call container.initialize() before using it.")

    return container.faiss_store
