from threading import Lock
from typing import Dict, List, Optional

from src.config.logging import get_logger
from src.schemas.registry import Chunk
from src.services.session_manager import SessionData
from src.services.vector_store.faiss_db import FAISSVectorStore

_lock = Lock()
_instance: Optional[FAISSVectorStore] = None
_instance_dimension: Optional[int] = None
_chunk_store: Dict[int, Chunk] = {}
_chunk_counter: int = 0


def get_faiss_vector_store(embedding_dimension: int) -> FAISSVectorStore:
    """Singleton FAISSVectorStore instance. If an instance already exists,
    the existing instance is returned. If the requested `embedding_dimension`
    differs from the already-created store, the existing instance will be
    used and a warning logged.

    Note: This function is deprecated for new code. Use SessionManager instead
    for per-session vector stores.
    """

    global _instance, _instance_dimension

    logger = get_logger("FAISSVectorStoreManager")

    with _lock:
        if _instance is None:
            logger.info(f"Creating FAISSVectorStore with dimension={embedding_dimension}")
            _instance = FAISSVectorStore(embedding_dimension=embedding_dimension)
            _instance_dimension = embedding_dimension
            return _instance

        if _instance_dimension != embedding_dimension:
            logger.warning(f"Requested FAISS dimension {embedding_dimension} does not match existing " f"dimension {_instance_dimension}. Using existing instance.")

        return _instance


def index_chunks(chunks: List[Chunk], session_id: Optional[str] = None) -> None:
    """Index a list of chunks into the shared chunk store or session-specific store."""

    global _chunk_counter

    logger = get_logger("FAISSVectorStoreManager")

    if session_id:
        # Per-session indexing - handled by SessionManager
        logger.debug(f"Per-session chunk indexing for session {session_id}")
        return

    with _lock:
        for chunk in chunks:
            _chunk_store[_chunk_counter] = chunk
            _chunk_counter += 1

        logger.info(f"Indexed {len(chunks)} chunks. Total chunks in store: {len(_chunk_store)}")


def get_chunk(chunk_index: int, session_id: Optional[str] = None) -> Optional[Chunk]:
    """Retrieve a chunk by its index from the shared store or session-specific store."""

    with _lock:
        return _chunk_store.get(chunk_index)


def get_chunks(chunk_indices: List[int], session_id: Optional[str] = None) -> List[Chunk]:
    """Retrieve multiple chunks by their indices."""

    with _lock:
        return [_chunk_store[idx] for idx in chunk_indices if idx in _chunk_store]


def get_all_chunks(session_id: Optional[str] = None) -> Dict[int, Chunk]:
    """Retrieve all indexed chunks."""

    with _lock:
        return _chunk_store.copy()


def reset_chunks(session_id: Optional[str] = None) -> None:
    """Clear all indexed chunks and reset counter."""

    global _chunk_counter

    logger = get_logger("FAISSVectorStoreManager")

    with _lock:
        _chunk_store.clear()
        _chunk_counter = 0
        logger.info("Chunk store reset.")


# Session-aware helper functions
def index_chunks_in_session(session_data: SessionData, chunks: List[Chunk], document_metadata: Optional[Dict] = None) -> None:
    """Index chunks into a specific session's chunk store and register document metadata.

    If chunks contain a `document_id` this function will create/update a document
    entry in `session_data.documents` containing metadata and the chunk indices
    that belong to that document.
    """

    logger = get_logger("FAISSVectorStoreManager")

    # determine document id
    document_id: Optional[str] = None
    if chunks:
        # prefer explicit document_id on chunks
        first = chunks[0]
        document_id = getattr(first, "document_id", None)

    if not document_id and document_metadata:
        document_id = document_metadata.get("document_id")

    # prepare document record
    if document_id:
        doc_record = session_data.documents.get(document_id, {"metadata": {}, "chunk_indices": []})
        # merge metadata if provided
        if document_metadata:
            doc_record["metadata"].update(document_metadata)
    else:
        doc_record = {"metadata": document_metadata or {}, "chunk_indices": []}

    # index chunks into the session chunk store and record their indices
    for chunk in chunks:
        session_data.chunk_store[session_data.chunk_counter] = chunk
        doc_record["chunk_indices"].append(session_data.chunk_counter)
        session_data.chunk_counter += 1

    # save document record if we have a document id
    if document_id:
        session_data.documents[document_id] = doc_record
        session_data.metadata["latest_document_id"] = document_id

    logger.info(f"Indexed {len(chunks)} chunks in session {session_data.session_id}. " f"Total chunks in session: {len(session_data.chunk_store)}")


def get_chunk_from_session(session_data: SessionData, chunk_index: int) -> Optional[Chunk]:
    """Retrieve a chunk from a specific session."""

    return session_data.chunk_store.get(chunk_index)


def get_chunks_from_session(session_data: SessionData, chunk_indices: List[int]) -> List[Chunk]:
    """Retrieve multiple chunks from a specific session."""

    return [session_data.chunk_store[idx] for idx in chunk_indices if idx in session_data.chunk_store]
