import pytest
from unittest.mock import MagicMock, AsyncMock
from src.services.retrieval.retrieval import RetrievalService
from src.services.session_manager import SessionData

@pytest.fixture
def mock_retrieval_service():
    service = RetrievalService()
    service.embedding_service = AsyncMock()
    service.vector_store = AsyncMock()
    service.llm = AsyncMock()
    # Mock rewrite_query to return empty list or just original query 
    # (actually implementation calls rewrite_query, so we should mock it to avoid LLM call)
    service.rewrite_query = AsyncMock(return_value=[]) 
    service.embedding_service.generate_embeddings = AsyncMock(return_value=[0.1, 0.2])
    return service

@pytest.fixture
def mock_session_data():
    session = MagicMock(spec=SessionData)
    session.vector_store = AsyncMock()
    return session

@pytest.mark.asyncio
async def test_dynamic_k_logic(mock_retrieval_service, mock_session_data):
    # Setup mock returns
    # We want to simulate a scenario where we have chunks with scores:
    # [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.80, 0.70]
    # default top_k=5 should return top 5.
    # dynamic_k=True should return more if the drop is small.
    
    mock_indices = [0, 1, 2, 3, 4, 5, 6, 7, 8]
    mock_scores = [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.80, 0.70]
    
    # Mocking search_index return
    mock_session_data.vector_store.search_index.return_value = {
        "indices": mock_indices,
        "scores": mock_scores,
        "search_time": 0.01
    }
    
    # Mock get_chunks_from_session (imported in retrieval.py)
    # We need to mock the global function or the lambda. 
    # Since retrieve_data defines `chunk_getter` inside, we need to mock what it calls.
    # It calls `get_chunks_from_session` imported from `src.services.vector_store.manager`.
    
    # We can patch it in the test module if we import it, or better, 
    # since we are unit testing the service, we can rely on how it constructs the chunk getter.
    # Actually, the easiest way might be to mock the `get_chunks_from_session` symbol in `src.services.retrieval.retrieval`.
    
    with pytest.patch('src.services.retrieval.retrieval.get_chunks_from_session') as mock_get_chunks:
        # Mock chunk return
        def get_chunk_side_effect(session, indices):
            idx = indices[0]
            chunk = MagicMock()
            chunk.content = f"Content {idx}"
            chunk.metadata = {}
            chunk.created_at = "now"
            return [chunk]
            
        mock_get_chunks.side_effect = get_chunk_side_effect

        # Test 1: Standard top_k=5
        result = await mock_retrieval_service.retrieve_data(
            query="test", 
            top_k=5, 
            dynamic_k=False, 
            session_data=mock_session_data
        )
        assert len(result["chunks"]) == 5
        assert result["chunks"][-1]["similarity_score"] == 0.95

        # Test 2: Dynamic k
        # With scores [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.80, ...]
        # 0.95 is the 5th item (last of base).
        # 0.94 >= 0.95 * 0.98 (0.931). Keep. Last = 0.94.
        # 0.93 >= 0.94 * 0.98 (0.9212). Keep. Last = 0.93.
        # 0.80 < 0.93 * 0.98 (0.9114). Drop.
        # So we expect 7 items.
        
        result_dynamic = await mock_retrieval_service.retrieve_data(
            query="test", 
            top_k=5, 
            dynamic_k=True, 
            session_data=mock_session_data
        )
        assert len(result_dynamic["chunks"]) == 7
        assert result_dynamic["chunks"][-1]["similarity_score"] == 0.93
