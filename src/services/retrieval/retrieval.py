from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.schemas.doc_chat import QueryRewriterResponse
from src.services.vector_store.manager import (
    get_chunks,
    get_chunks_from_session,
    get_faiss_vector_store,
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "v1"
_QUERY_REWRITER_SYSTEM = (_PROMPTS_DIR / "query_rewriter_system.mustache").read_text(encoding="utf-8")
_QUERY_REWRITER_USER = (_PROMPTS_DIR / "query_rewriter_user.mustache").read_text(encoding="utf-8")


logger = get_logger(__name__)


class RetrievalService:
    """Retrieval Service for retrieving the data."""

    def __init__(self) -> None:
        super().__init__()

        from src.core.container import (
            get_bedrock_model,
            get_embedding_service,
            get_session_manager,
        )

        self.settings = get_settings()

        self.embedding_service = get_embedding_service()
        self.llm = get_bedrock_model()
        self.session_manager = get_session_manager()
        self.vector_store = get_faiss_vector_store(self.embedding_service.get_embedding_dimensions())

    async def rewrite_query(self, query: str, session_id: str) -> List[str]:
        """Rewrite the given query."""

        context: Dict[str, Any] = {
            "query": query,
        }
        logger.info("Rewriting query", original_query=query, session_id=session_id)
        response: QueryRewriterResponse = await self.llm.generate(
            prompt=_QUERY_REWRITER_USER,
            context=context,
            response_model=QueryRewriterResponse,
            session_id=session_id,
            system_message=_QUERY_REWRITER_SYSTEM,
        )
        return [q.query for q in response.queries]

    async def retrieve_document(self) -> Dict[str, Any]:
        """Retrieve the whole document chunks."""
        return {}

    async def retrieve_data(self, query: str, session_id: str, top_k: int = 5, dynamic_k: bool = False, threshold: Optional[float] = 0.0) -> Dict[str, Any]:
        """Retrieve and return relevant document chunks based on query."""

        if not query or not query.strip():
            raise ValueError("Query cannot be empty.")

        try:
            queries = await self.rewrite_query(query=query, session_id=session_id)
            # queries = [query]

            all_hits: Dict[int, Dict[str, Any]] = {}

            initial_k = max(10, top_k * 2) if dynamic_k else top_k

            for query_rewriten in queries:
                new_query = query + " | " + query_rewriten
                # Generate query embedding
                logger.info("Generating embedding for the query", new_query=new_query, session_id=session_id)
                query_embedding = await self.embedding_service.generate_embeddings(text=new_query, task="retrieval.query", session_id=session_id)

                # Get session data
                session_data = self.session_manager.get_session(session_id)
                if not session_data:
                    return {"error": "Session not found. Please ingest documents first.", "session_id": session_id}

                # Search vector store for top-k similar embeddings
                if session_data:
                    # Per-session search
                    search_result = await session_data.vector_store.search_index(query_embedding, session_id=session_id, top_k=initial_k)
                    print(search_result)
                    if not search_result:
                        logger.info("No search results found in session vector store", session_id=session_id)
                        return {"error": "Session not found. Please ingest documents first.", "session_id": session_id}
                    logger.info("Searching for similar chunks in session", session_id=session_id, query=new_query)
                    chunk_getter = lambda idx: get_chunks_from_session(session_data, [idx])  # noqa: E731
                else:
                    # Global search (legacy)
                    search_result = await self.vector_store.search_index(query_embedding=query_embedding, session_id=session_id, top_k=initial_k)
                    logger.info("Searching for similar chunks in global vector store", query=new_query, session_id=session_id)
                    chunk_getter = lambda idx: get_chunks([idx])  # noqa: E731

                indices = search_result.get("indices", [])
                scores = search_result.get("scores", [])

                # Fetch chunks from the manager by their indices
                for idx, score in zip(indices, scores):
                    if threshold is not None and score < threshold:
                        logger.debug("Skipping result with score below threshold", score=score, threshold=threshold, session_id=session_id)
                        continue

                    if idx not in all_hits or score > all_hits[idx]["similarity_score"]:
                        chunk = chunk_getter(idx)
                        if not chunk:
                            continue
                        all_hits[idx] = {
                            "index": idx,
                            "content": chunk[0].content,
                            "similarity_score": float(score),
                            "metadata": chunk[0].metadata,
                            "created_at": chunk[0].created_at,
                            "matched_query": new_query,
                        }

            ranked_chunks = sorted(
                all_hits.values(),
                key=lambda x: x["similarity_score"],
                reverse=True,
            )

            # Apply dynamic top-k logic or standard top-k
            final_chunks = []
            if dynamic_k and ranked_chunks:
                # Always keep the standard top_k at minimum (if available)
                base_chunks = ranked_chunks[:top_k]
                final_chunks.extend(base_chunks)

                # Check remaining chunks
                remaining_chunks = ranked_chunks[top_k:]
                if base_chunks and remaining_chunks:
                    last_score = base_chunks[-1]["similarity_score"]

                    for chunk in remaining_chunks:  # type: ignore
                        current_score = chunk["similarity_score"]  # type: ignore

                        # Relative Drop Check
                        if current_score >= last_score * 0.98:  # 2% drop tolerance
                            final_chunks.append(chunk)  # type: ignore
                            last_score = current_score
                        else:
                            # Drop is too steep, stop here
                            break

                        # Safety break to avoid returning everything if scores are flat
                        if len(final_chunks) >= top_k * 2:
                            break
            else:
                final_chunks = ranked_chunks[:top_k]

            logger.info("Retrieved chunks for query", num_chunks=len(final_chunks), query=query, session_id=session_id)

            return {
                "query": query,
                "rewritten_queries": queries,
                "chunks": final_chunks,
                "num_results": len(final_chunks),
                "search_metadata": {
                    "search_time": search_result.get("search_time", 0),
                    "requested_top_k": top_k,
                    "dynamic_k_enabled": dynamic_k,
                    "returned_results": len(final_chunks),
                },
            }

        except Exception as e:
            logger.error("Error retrieving data", error=str(e), query=query, session_id=session_id)
            raise ValueError("Unable to retrieve the data.") from e
