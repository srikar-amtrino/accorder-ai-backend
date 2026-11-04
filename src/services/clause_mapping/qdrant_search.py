import asyncio
from typing import Any, Dict, List, Optional, Sequence

from src.config.logging import get_logger
from src.schemas.clause_mapping import ClauseMatch
from src.services.clause_mapping.search import SEARCH_TOP_K, BaseClauseSearch
from src.services.vector_store.embeddings.embedding_client import EmbeddingClient
from src.services.vector_store.qdrant_db import search_points_batch

logger = get_logger(__name__)

# Clause texts sent to the embedding service per request. The service rejects
# more than 16 texts per call (HTTP 422 "List should have at most 16 items"),
# so this is a hard ceiling, not a tuning knob; larger inputs are sent in
# consecutive calls by EmbeddingClient.
EMBED_BATCH_SIZE = 16

# The service may be slow, but a batch of short texts takes seconds; a call
# taking longer than this means the service is unhealthy and the review should
# fall back to the offline classifier.
EMBED_TIMEOUT_SECONDS = 60.0

# Only typed clause records can tell us a clause type; headings, exhibits, and
# other non-clause content in the collection are skipped, as are the pipeline's
# placeholder labels that carry no clause type.
_PLACEHOLDER_TYPES = ["new-clause-v1", "other-non-clause-content", "template-placeholder"]
_CLAUSE_RECORDS_FILTER: Dict[str, Any] = {
    "must": [
        {"key": "label", "match": {"value": "Clause"}},
        {"key": "is_exhibit", "match": {"value": False}},
    ],
    "must_not": [
        {"key": "canonical_type", "match": {"any": _PLACEHOLDER_TYPES}},
    ],
}


class QdrantClauseSearch(BaseClauseSearch):
    """Clause search over the shared Qdrant collection, embedding clauses with the pipeline's embedding service.

    The query embedding must come from the same model that built the collection
    (bge-m3), so clause texts go through the remote embedding service rather than
    the local MiniLM model used elsewhere in the app.
    """

    def __init__(self, embedding_client: Optional[EmbeddingClient] = None) -> None:
        self._embedding_client = embedding_client or EmbeddingClient(timeout=EMBED_TIMEOUT_SECONDS, batch_size=EMBED_BATCH_SIZE)

    async def search(self, texts: Sequence[str], agreement_type: Optional[str], session_id: str, top_k: int = SEARCH_TOP_K) -> List[List[ClauseMatch]]:
        """Return the top_k typed hits for each text, in input order.

        Texts are embedded as given; callers trim them to the clause heading
        and its first operative words (see build_smart_embedding_query).

        agreement_type is not yet applied as a Qdrant filter: requests send free
        text ("MSA") while records store a display name ("Master Service
        Agreement (MSA)"), so a value filter would silently drop every hit. The
        policy store narrows by agreement type instead.
        """

        if not texts:
            return []

        vectors = await self._embedding_client.embed([" ".join(text.split()) for text in texts])
        results = await asyncio.to_thread(search_points_batch, vectors, top_k, None, _CLAUSE_RECORDS_FILTER)

        hits: List[List[ClauseMatch]] = []
        for text, points in zip(texts, results):
            matches = [match for match in (_to_match(point) for point in points) if match is not None]
            hits.append(matches)
            top = matches[0] if matches else None
            logger.debug(
                "Clause search hits.",
                session_id=session_id,
                clause=" ".join(text.split()[:8]),
                hits=len(matches),
                top_type=top.canonical_type if top else None,
                top_sub_type=top.sub_type if top else None,
                top_score=round(top.score, 3) if top else None,
            )
        return hits


def _to_match(point: Dict[str, Any]) -> Optional[ClauseMatch]:
    """Build a ClauseMatch from a Qdrant point, or None when the record has no usable clause type."""

    payload = point.get("payload") or {}
    canonical_type = payload.get("canonical_type")
    score = point.get("score")
    if not isinstance(canonical_type, str) or not canonical_type.strip() or not isinstance(score, (int, float)):
        return None
    sub_type = payload.get("sub_type")
    return ClauseMatch(
        canonical_type=canonical_type.strip(),
        sub_type=sub_type.strip() if isinstance(sub_type, str) and sub_type.strip() else None,
        score=float(score),
    )
