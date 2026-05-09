import asyncio
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional

from src.config.logging import Logger
from src.config.settings import get_settings
from src.schemas.registry import Chunk
from src.services.vector_store.faiss_db import FAISSVectorStore

# Session management system for handling per-session data stores and TTL-based cleanup.


@dataclass
class SessionData:
    """Data container for a single session."""

    session_id: str
    created_at: float
    last_access: float
    vector_store: FAISSVectorStore
    chunk_store: Dict[int, Chunk] = field(default_factory=dict)
    chunk_counter: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # documents keyed by document_id containing metadata and chunk indices
    documents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Store tool results to avoid regenerating them
    tool_results: Dict[str, Any] = field(default_factory=dict)

    def refresh_access(self) -> None:
        """Update the last access timestamp."""
        self.last_access = time.time()

    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if session has expired based on TTL."""
        return (time.time() - self.last_access) > ttl_seconds


class SessionManager(Logger):
    """Manages per-session in-memory data stores with automatic TTL-based cleanup.

    Each session has its own:
    - FAISS vector store for embeddings
    - Chunk store for documents
    - Metadata dictionary
    - TTL timer and access tracking

    """

    def __init__(self, embedding_dimension: int = 1536) -> None:
        """Initialize the session manager."""

        super().__init__()
        self.settings = get_settings()
        self.embedding_dimension = embedding_dimension

        # Session storage
        self._sessions: Dict[str, SessionData] = {}
        self._lock = Lock()

        # TTL settings (in seconds)
        self.ttl_seconds = self.settings.session_ttl_minutes * 60
        self.cleanup_interval_seconds = self.settings.session_cleanup_interval_minutes * 60

        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._shutdown_event: asyncio.Event = asyncio.Event()

    def get_or_create_session(self, session_id: str) -> SessionData:
        """Get an existing session or create a new one."""

        with self._lock:
            if session_id not in self._sessions:
                self.logger.info(f"Creating new session: {session_id}")
                current_time = time.time()
                session = SessionData(
                    session_id=session_id,
                    created_at=current_time,
                    last_access=current_time,
                    vector_store=FAISSVectorStore(embedding_dimension=self.embedding_dimension),
                )
                self._sessions[session_id] = session
            else:
                # Refresh access time on retrieval
                self._sessions[session_id].refresh_access()

            return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """Get an existing session without creating one."""

        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.refresh_access()
            return session

    def refresh_session(self, session_id: str) -> None:
        """Refresh the access timestamp for a session."""

        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].refresh_access()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its data."""

        with self._lock:
            if session_id in self._sessions:
                self.logger.info(f"Deleting session: {session_id}")
                del self._sessions[session_id]
                return True
            return False

    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a session."""

        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None

            time_since_access = time.time() - session.last_access
            return {
                "session_id": session_id,
                "created_at": session.created_at,
                "last_access": session.last_access,
                "idle_seconds": time_since_access,
                "chunks_indexed": len(session.chunk_store),
                "documents": [
                    {
                        "document_id": doc_id,
                        "chunk_count": len(doc.get("chunk_indices", [])),
                        "metadata": doc.get("metadata", {}),
                    }
                    for doc_id, doc in session.documents.items()
                ],
                "tool_results": session.tool_results,
                "vectors_added": session.vector_store.stats["vectors_added"],
                "is_expired": session.is_expired(self.ttl_seconds),
            }

    def list_sessions(self) -> List[Dict]:
        """Get information about all active sessions."""

        with self._lock:
            return [
                {
                    "session_id": sid,
                    "idle_seconds": time.time() - session.last_access,
                    "chunks_indexed": len(session.chunk_store),
                    "is_expired": session.is_expired(self.ttl_seconds),
                }
                for sid, session in self._sessions.items()
            ]

    def get_document_info(self, session_id: str, document_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve information about a specific document stored in a session.

        Returns metadata, chunk indices, and the actual chunks for convenience.
        """

        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None

            doc = session.documents.get(document_id)
            if not doc:
                return None

            chunk_indices = doc.get("chunk_indices", [])
            chunks = [session.chunk_store[idx] for idx in chunk_indices if idx in session.chunk_store]

            return {
                "document_id": document_id,
                "metadata": doc.get("metadata", {}),
                "chunk_count": len(chunk_indices),
                "chunk_indices": chunk_indices,
                "chunks": chunks,
            }

    async def cleanup_expired_sessions(self) -> int:
        """Check for and delete expired sessions."""

        # Collect expired session ids without holding lock while performing
        # potentially re-entrant operations.
        with self._lock:
            expired_sessions = [sid for sid, session in self._sessions.items() if session.is_expired(self.ttl_seconds)]

        cleaned_count = 0
        for session_id in expired_sessions:
            # Acquire lock per-session to safely remove it
            with self._lock:
                session = self._sessions.get(session_id)
                if not session:
                    continue
                idle_hours = (time.time() - session.last_access) / 3600
                self.logger.info(f"Deleting expired session {session_id} " f"(idle {idle_hours:.1f}h, TTL {self.ttl_seconds/3600:.1f}h)")
                del self._sessions[session_id]
                cleaned_count += 1

        if cleaned_count:
            self.logger.info(f"Cleaned up {cleaned_count} expired sessions")

        return cleaned_count

    async def start_cleanup_worker(self) -> None:
        """Start the background cleanup worker."""

        self.logger.info(f"Starting session cleanup worker " f"(TTL: {self.ttl_seconds/3600:.1f}h, " f"check interval: {self.cleanup_interval_seconds/60:.0f}m)")
        self._shutdown_event.clear()

        try:
            while not self._shutdown_event.is_set():
                try:
                    # Wait for cleanup interval or shutdown
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=self.cleanup_interval_seconds)
                    # If we get here, shutdown was signaled
                    break
                except asyncio.TimeoutError:
                    # Timeout occurred, perform cleanup
                    await self.cleanup_expired_sessions()
        except Exception as e:
            self.logger.error(f"Error in cleanup worker: {str(e)}")
        finally:
            self.logger.info("Session cleanup worker stopped")

    def start_cleanup_worker_sync(self) -> None:
        """Start cleanup worker synchronously (for FastAPI startup)."""

        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self.start_cleanup_worker())

    async def stop_cleanup_worker(self) -> None:
        """Stop the background cleanup worker."""

        self.logger.info("Stopping session cleanup worker...")
        self._shutdown_event.set()

        if self._cleanup_task:
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=5)
            except asyncio.TimeoutError:
                self.logger.warning("Cleanup worker did not stop within timeout")
                self._cleanup_task.cancel()

    def get_total_stats(self) -> Dict:
        """Get statistics about all sessions."""

        with self._lock:
            total_chunks = sum(len(s.chunk_store) for s in self._sessions.values())
            total_vectors = sum(s.vector_store.stats["vectors_added"] for s in self._sessions.values())

            return {
                "total_sessions": len(self._sessions),
                "total_chunks_indexed": total_chunks,
                "total_vectors_added": total_vectors,
                "ttl_minutes": self.settings.session_ttl_minutes,
                "cleanup_interval_minutes": self.settings.session_cleanup_interval_minutes,
            }
