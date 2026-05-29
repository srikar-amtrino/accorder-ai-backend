import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, List, Optional

from src.config.logging import Logger
from src.schemas.registry import Chunk
from src.services.vector_store.faiss_db import FAISSVectorStore


@dataclass
class SessionData:
    """Container for all session-specific data."""

    session_id: str
    created_at: float
    last_access: float

    vector_store: FAISSVectorStore

    chunk_store: Dict[int, Chunk] = field(default_factory=dict)
    chunk_counter: int = 0

    metadata: Dict[str, Any] = field(default_factory=dict)

    documents: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    tool_results: Dict[str, Any] = field(default_factory=dict)

    def refresh_access(self) -> None:
        self.last_access = time.time()


class SessionManager(Logger):
    """
    Production-ready in-memory session manager.

    Responsibilities:
    - Maintain per-session vector stores
    - Store chunks/documents
    - Thread-safe access
    - Explicit lifecycle management

    NOTE:
    This is still in-memory only.
    For multi-instance deployments use Redis/Postgres.
    """

    def __init__(
        self,
        embedding_dimension: int = 1536,
        max_sessions: int = 1000,
    ) -> None:
        super().__init__()

        self.embedding_dimension = embedding_dimension
        self.max_sessions = max_sessions

        self._sessions: Dict[str, SessionData] = {}

        self._lock = RLock()

    def create_session(self, session_id: str) -> SessionData:
        """Create a brand-new session."""

        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session already exists: {session_id}")

            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("Maximum session limit reached")

            current_time = time.time()

            session = SessionData(
                session_id=session_id,
                created_at=current_time,
                last_access=current_time,
                vector_store=FAISSVectorStore(embedding_dimension=self.embedding_dimension),
            )

            self._sessions[session_id] = session

            self.logger.info(f"Created session: {session_id}")

            return session

    def get_or_create_session(self, session_id: str) -> SessionData:
        """Get existing session or create one."""

        with self._lock:
            session = self._sessions.get(session_id)

            if session:
                session.refresh_access()
                return session

            return self.create_session(session_id)

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """Retrieve session."""

        with self._lock:
            session = self._sessions.get(session_id)

            if session:
                session.refresh_access()

            return session

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def delete_session(self, session_id: str) -> bool:
        """Explicitly delete session."""

        with self._lock:
            session = self._sessions.pop(session_id, None)

            if not session:
                return False

            self.logger.info(f"Deleted session: {session_id}")

            return True

    def clear_all_sessions(self) -> None:
        """Delete all sessions."""

        with self._lock:
            count = len(self._sessions)

            self._sessions.clear()

            self.logger.warning(f"Cleared all sessions ({count})")

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all active sessions."""

        with self._lock:
            return [
                {
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "last_access": session.last_access,
                    "chunks_indexed": len(session.chunk_store),
                    "documents": len(session.documents),
                }
                for session in self._sessions.values()
            ]

    def get_session_info(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Detailed session information."""

        with self._lock:
            session = self._sessions.get(session_id)

            if not session:
                return None

            return {
                "session_id": session.session_id,
                "created_at": session.created_at,
                "last_access": session.last_access,
                "chunks_indexed": len(session.chunk_store),
                "documents": list(session.documents.keys()),
                "tool_results": list(session.tool_results.keys()),
                "vectors_added": session.vector_store.stats.get(
                    "vectors_added",
                    0,
                ),
            }

    def get_document_info(
        self,
        session_id: str,
        document_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get stored document details."""

        with self._lock:
            session = self._sessions.get(session_id)

            if not session:
                return None

            document = session.documents.get(document_id)

            if not document:
                return None

            chunk_indices = document.get("chunk_indices", [])

            chunks = [session.chunk_store[idx] for idx in chunk_indices if idx in session.chunk_store]

            return {
                "document_id": document_id,
                "metadata": document.get("metadata", {}),
                "chunk_count": len(chunk_indices),
                "chunk_indices": chunk_indices,
                "chunks": chunks,
            }

    def get_total_stats(self) -> Dict[str, Any]:
        """Global manager statistics."""

        with self._lock:
            total_chunks = sum(len(session.chunk_store) for session in self._sessions.values())

            total_vectors = sum(session.vector_store.stats.get("vectors_added", 0) for session in self._sessions.values())

            return {
                "total_sessions": len(self._sessions),
                "total_chunks_indexed": total_chunks,
                "total_vectors_added": total_vectors,
                "max_sessions": self.max_sessions,
            }
