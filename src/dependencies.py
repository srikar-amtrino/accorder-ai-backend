from typing import Optional

from src.config.logging import Logger
from src.config.settings import Settings
from src.services.ingestion.ingestion import IngestionService
from src.services.llm.azure_openai_model import AzureOpenAIModel

# from src.services.llm.gemini_model import GeminiModel
from src.services.retrieval.retrieval import RetrievalService
from src.services.session_manager import SessionManager

# from src.services.vector_store.embeddings.embedding_service import BGEEmbeddingService
# from src.services.vector_store.embeddings.jina_embeddings import JinaEmbeddings
from src.services.vector_store.embeddings.embedding_service import (
    HuggingFaceEmbeddingService,
)
from src.services.vector_store.faiss_db import FAISSVectorStore


class ServiceContainer(Logger):
    """
    Service container for managing application-wide service initialization.

    Services are initialized once at application startup and reused throughout
    the application lifecycle. This ensures services remain stateless and that
    failures in one service do not affect others.
    """

    def __init__(self) -> None:
        """Initialize the service container."""
        super().__init__()

        # Service instances
        self._ingestion_service: Optional[IngestionService] = None
        self._retrieval_service: Optional[RetrievalService] = None
        self._azure_openai_model: Optional[AzureOpenAIModel] = None
        # self._gemini_model: Optional[GeminiModel] = None
        # self._embedding_service: Optional[BGEEmbeddingService] = None
        # self._embedding_service: Optional[JinaEmbeddings] = None
        self._embedding_service: Optional[HuggingFaceEmbeddingService] = None
        self._session_manager: Optional[SessionManager] = None
        self._faiss_store: Optional[FAISSVectorStore] = None
        self._settings: Optional[Settings] = None

    def initialize(self) -> None:
        """Initialize all services at application startup."""
        try:
            self.logger.info("Initializing service container...")

            # Initialize embedding service first to get correct dimensions
            # self._embedding_service = BGEEmbeddingService()
            # self._embedding_service = JinaEmbeddings()
            self._embedding_service = HuggingFaceEmbeddingService()
            embedding_dimension = self._embedding_service.get_embedding_dimensions()
            self.logger.info(f"BGEEmbeddingService initialized with dimension {embedding_dimension}")

            # Initialize session manager with correct embedding dimensions
            self._session_manager = SessionManager(embedding_dimension=embedding_dimension)
            self.logger.info("SessionManager initialized")

            # Initialize LLM models
            self._azure_openai_model = AzureOpenAIModel()
            self.logger.info("AzureOpenAIModel initialized")

            # self._gemini_model = GeminiModel()
            # self.logger.info("GeminiModel initialized")

            # Initialize FAISS Store
            self._faiss_store = FAISSVectorStore(self._embedding_service.get_embedding_dimensions())
            self.logger.info("FAISS Store initialized")

            # Initialize retrieval service
            self._retrieval_service = RetrievalService()
            self.logger.info("RetrievalService initialized")

            # Initialize ingestion service
            self._ingestion_service = IngestionService()
            self.logger.info("IngestionService initialized")

            # Initialize settings
            self._settings = Settings()
            self.logger.info("Settings Initialized.")

            self.logger.info("Service container initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize service container: {str(e)}")
            raise

    async def shutdown(self) -> None:
        """Shutdown all services at application shutdown (async-safe)."""
        try:
            self.logger.info("Shutting down service container...")

            # Stop the cleanup worker
            if self._session_manager:
                try:
                    await self._session_manager.stop_cleanup_worker()
                except Exception as e:
                    self.logger.error(f"Error stopping cleanup worker: {str(e)}")

            # Services are cleaned up here
            self._ingestion_service = None
            self._retrieval_service = None
            self._azure_openai_model = None
            self._gemini_model = None
            self._embedding_service = None
            self._session_manager = None
            self._settings = None

            self.logger.info("Service container shutdown complete")

        except Exception as e:
            self.logger.error(f"Error during service container shutdown: {str(e)}")

    # Service getters
    @property
    def settings(self) -> Settings:
        """Get the settings instance"""
        if self._settings is None:
            raise RuntimeError("Settings is not initialized. Call initialize() first.")
        return self._settings

    @property
    def session_manager(self) -> SessionManager:
        """Get the session manager instance."""
        if self._session_manager is None:
            raise RuntimeError("SessionManager not initialized. Call initialize() first.")
        return self._session_manager

    @property
    def faiss_store(self) -> FAISSVectorStore:
        """Get the FAISS store instance."""
        if self._faiss_store is None:
            raise RuntimeError("SessionManager not initialized. Call initialize() first.")
        return self._faiss_store

    @property
    def ingestion_service(self) -> IngestionService:
        """Get the ingestion service instance."""
        if self._ingestion_service is None:
            raise RuntimeError("IngestionService not initialized. Call initialize() first.")
        return self._ingestion_service

    @property
    def retrieval_service(self) -> RetrievalService:
        """Get the retrieval service instance."""
        if self._retrieval_service is None:
            raise RuntimeError("RetrievalService not initialized. Call initialize() first.")
        return self._retrieval_service

    @property
    def azure_openai_model(self) -> AzureOpenAIModel:
        """Get the Azure OpenAI model instance."""
        if self._azure_openai_model is None:
            raise RuntimeError("AzureOpenAIModel not initialized. Call initialize() first.")
        return self._azure_openai_model

    # @property
    # def gemini_model(self) -> GeminiModel:
    #     """Get the Gemini model instance."""
    #     if self._gemini_model is None:
    #         raise RuntimeError("GeminiModel not initialized. Call initialize() first.")
    #     return self._gemini_model

    @property
    # def bge_embedding_service(self) -> BGEEmbeddingService:
    # def bge_embedding_service(self) -> JinaEmbeddings:
    def embedding_service(self) -> HuggingFaceEmbeddingService:
        """Get the BGE embedding service instance."""
        if self._embedding_service is None:
            raise RuntimeError("BGEEmbeddingService not initialized. Call initialize() first.")
        return self._embedding_service


# Global service container instance
_service_container: Optional[ServiceContainer] = None


def get_service_container() -> ServiceContainer:
    """Get the global service container instance."""
    global _service_container
    if _service_container is None:
        _service_container = ServiceContainer()
    return _service_container


async def initialize_dependencies() -> ServiceContainer:
    """Initialize all dependencies at application startup."""

    container = get_service_container()
    container.initialize()

    # Start cleanup worker in the running event loop if session manager exists
    if container._session_manager:
        try:
            # Use SessionManager helper to create background task
            container._session_manager.start_cleanup_worker_sync()
            container.logger.info("Session cleanup worker started")
        except RuntimeError:
            # No running loop; caller must ensure cleanup worker is started
            container.logger.warning("Could not start cleanup worker: no running event loop")

    return container


async def shutdown_dependencies() -> None:
    """Shutdown all dependencies at application shutdown."""

    container = get_service_container()
    await container.shutdown()
