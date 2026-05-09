from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.logging import Logger
from src.config.settings import get_settings
from src.schemas.doc_chat import QueryRewriterResponse
from src.services.llm.base_model import BaseLLMModel
from src.services.session_manager import SessionData
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)
from src.services.vector_store.manager import (
    get_chunks,
    get_chunks_from_session,
    get_faiss_vector_store,
)


class RetrievalService(Logger):
    """Retrieval Service for retrieving the data."""

    def __init__(self) -> None:
        super().__init__()

        self.settings = get_settings()
        from src.dependencies import get_service_container

        service_container = get_service_container()
        self.embedding_service: BaseEmbeddingService = service_container.embedding_service
        self.llm: BaseLLMModel = service_container.azure_openai_model
        self.rewrite_query_prompt = Path(r"src\services\prompts\v1\query_rewriter.mustache").read_text()
        self.vector_store = get_faiss_vector_store(self.embedding_service.get_embedding_dimensions())

    async def rewrite_query(self, query: str) -> List[str]:
        """Rewrite the given query."""

        context: Dict[str, Any] = {
            "query": query,
        }
        self.logger.info(f"Rewriting query: {query}")
        response: QueryRewriterResponse = await self.llm.generate(prompt=self.rewrite_query_prompt, context=context, response_model=QueryRewriterResponse)
        return [q.query for q in response.queries]

    async def retrieve_document(self) -> Dict[str, Any]:
        """Retrieve the whole document chunks."""
        return {}

    async def retrieve_data(self, query: str, top_k: int = 5, dynamic_k: bool = False, threshold: Optional[float] = 0.0, session_data: Optional[SessionData] = None) -> Dict[str, Any]:
        """Retrieve and return relevant document chunks based on query."""

        if not query or not query.strip():
            raise ValueError("Query cannot be empty.")

        try:
            queries = await self.rewrite_query(query=query)
            # queries = [query]
            self.logger.info(f"Generated {len(queries)} rewritten queries for the original query: '{query}'")

            all_hits: Dict[int, Dict[str, Any]] = {}

            # If dynamic_k is enabled, we fetch more initial candidates to filter down
            # If standard top_k is small, we want enough candidates to find the drop-off
            initial_k = max(10, top_k * 2) if dynamic_k else top_k

            for query_rewriten in queries:
                new_query = query + " | " + query_rewriten
                # Generate query embedding
                self.logger.info(f"Generating embedding for the query: '{new_query}'")
                query_embedding = await self.embedding_service.generate_embeddings(text=new_query, task="retrieval.query")

                # Search vector store for top-k similar embeddings
                if session_data:
                    # Per-session search
                    search_result = await session_data.vector_store.search_index(query_embedding, initial_k)
                    self.logger.info(f"Searching for similar chunks in session {session_data.session_id} for query: '{new_query}'")
                    chunk_getter = lambda idx: get_chunks_from_session(session_data, [idx])  # noqa: E731
                else:
                    # Global search (legacy)
                    search_result = await self.vector_store.search_index(query_embedding, initial_k)
                    self.logger.info(f"Searching for similar chunks in global vector store for query: '{new_query}'")
                    chunk_getter = lambda idx: get_chunks([idx])  # noqa: E731

                indices = search_result.get("indices", [])
                scores = search_result.get("scores", [])

                # Fetch chunks from the manager by their indices
                for idx, score in zip(indices, scores):
                    if threshold is not None and score < threshold:
                        self.logger.debug(f"Skipping result with score {score} (below threshold {threshold})")
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

                    # Threshold: Score must be within X% of the last included chunk
                    # or drop-off shouldn't be too steep.
                    # Simple heuristic: If the next chunk is at least 95% as relevant as the last one, keep it.
                    # We can also check against the very first chunk to ensure overall relevance.

                    for chunk in remaining_chunks:
                        current_score = chunk["similarity_score"]

                        # Relative Drop Check
                        if current_score >= last_score * 0.98:  # 2% drop tolerance
                            final_chunks.append(chunk)
                            last_score = current_score  # Update reference? Or keep strict reference?
                            # Updating reference allows a gentle slope. Keeping strict reference enforces a hard shelf.
                            # Let's update to allow gentle slope but maybe set a max limit?
                        else:
                            # Drop is too steep, stop here
                            break

                        # Safety break to avoid returning everything if scores are flat
                        if len(final_chunks) >= top_k * 2:
                            break
            else:
                final_chunks = ranked_chunks[:top_k]

            self.logger.info(f"Retrieved {len(final_chunks)} chunks for query (requested top_k={top_k}, dynamic={dynamic_k})")

            return {
                "query": new_query,
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
            self.logger.error(f"Error retrieving data: {str(e)}")
            raise ValueError("Unable to retrieve the data.") from e
