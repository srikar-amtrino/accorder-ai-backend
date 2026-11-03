import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv(
    "QDRANT_URL",
    "http://18.145.28.145:6333",
).rstrip("/")

QDRANT_COLLECTION = os.getenv(
    "QDRANT_COLLECTION",
    "legal_clauses_v1",
)

QDRANT_TIMEOUT_SECONDS = float(
    os.getenv("QDRANT_TIMEOUT_SECONDS", "30")
)

QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

BATCH_SIZE = 64


class QdrantError(RuntimeError):
    """Raised when a Qdrant operation fails."""


def _get_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}

    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    return headers


def _request(
    method: str,
    path: str,
    **kwargs: Any,
) -> requests.Response:
    try:
        response = requests.request(
            method=method,
            url=f"{QDRANT_URL}{path}",
            headers=_get_headers(),
            timeout=QDRANT_TIMEOUT_SECONDS,
            **kwargs,
        )
        response.raise_for_status()
        return response

    except requests.RequestException as exc:
        response = exc.response
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = response.text

            if isinstance(body, dict):
                detail = body.get("status", body)
                if isinstance(detail, dict):
                    detail = detail.get("error", detail)
            else:
                detail = body

            detail_text = str(detail).strip()[:1000]
            logger.error(
                "Qdrant request failed: %s %s (HTTP %s): %s",
                method,
                path,
                response.status_code,
                detail_text,
                exc_info=True,
            )
            raise QdrantError(
                f"Qdrant request failed: {method} {path} "
                f"(HTTP {response.status_code}): {detail_text}"
            ) from exc

        logger.exception("Qdrant request failed: %s %s", method, path)
        raise QdrantError(f"Qdrant request failed: {method} {path}") from exc


def ensure_collection(vector_size: int) -> None:
    """Create the collection if it does not exist."""

    if vector_size <= 0:
        raise ValueError("Vector size must be greater than zero.")

    collection_path = f"/collections/{QDRANT_COLLECTION}"

    try:
        response = requests.get(
            f"{QDRANT_URL}{collection_path}",
            headers=_get_headers(),
            timeout=QDRANT_TIMEOUT_SECONDS,
        )

        if response.status_code == 200:
            collection_info = response.json().get("result", {})
            configured_vectors = collection_info.get("config", {}).get(
                "params", {}
            ).get("vectors", {})

            if isinstance(configured_vectors, dict):
                dense_config = configured_vectors.get("dense")

                if dense_config is None:
                    raise QdrantError(
                        f"Collection '{QDRANT_COLLECTION}' does not "
                        "have the named vector 'dense'."
                    )

                existing_size = dense_config.get("size")

                if existing_size != vector_size:
                    raise QdrantError(
                        f"Collection vector size is {existing_size}, "
                        f"but the embedding service returned {vector_size}."
                    )

            logger.info("Qdrant collection already exists.")
            return

        if response.status_code != 404:
            response.raise_for_status()

    except requests.RequestException as exc:
        raise QdrantError("Could not check the Qdrant collection.") from exc

    _request(
        "PUT",
        collection_path,
        json={
            "vectors": {
                "dense": {
                    "size": vector_size,
                    "distance": "Cosine",
                }
            }
        },
    )

    logger.info(
        "Created Qdrant collection '%s' with vector size %s.",
        QDRANT_COLLECTION,
        vector_size,
    )


def upsert_vectors(points: list[dict[str, Any]]) -> int:
    """Insert new points or replace existing points with matching IDs."""

    if not points:
        return 0

    for start in range(0, len(points), BATCH_SIZE):
        batch = points[start : start + BATCH_SIZE]

        _request(
            "PUT",
            f"/collections/{QDRANT_COLLECTION}/points?wait=true",
            json={"points": batch},
        )

    logger.info("Upserted %s points into Qdrant.", len(points))
    return len(points)


def search_points(
    vector: list[float],
    limit: int = 5,
    score_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Return the most similar points from the configured Qdrant collection."""

    if not vector:
        raise ValueError("Query vector cannot be empty.")
    if limit <= 0:
        raise ValueError("Limit must be greater than zero.")

    payload: dict[str, Any] = {
        "vector": {"name": "dense", "vector": vector},
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }
    if score_threshold is not None:
        payload["score_threshold"] = score_threshold

    response = _request(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/search",
        json=payload,
    )

    try:
        body = response.json()
    except ValueError as exc:
        logger.exception("Qdrant returned an invalid JSON response while searching points.")
        raise QdrantError("Qdrant returned an invalid response.") from exc

    if not isinstance(body, dict):
        logger.error("Qdrant returned an invalid JSON response while searching points.")
        raise QdrantError("Qdrant returned an invalid response.")

    result = body.get("result")
    if not isinstance(result, list) or not all(isinstance(point, dict) for point in result):
        logger.error("Qdrant returned an invalid point search result.")
        raise QdrantError("Qdrant returned an invalid point search result.")

    return result


def search_points_batch(
    vectors: list[list[float]],
    limit: int = 5,
    score_threshold: float | None = None,
    query_filter: dict[str, Any] | None = None,
) -> list[list[dict[str, Any]]]:
    """Run one similarity search per vector in a single request.

    Returns one list of points per input vector, in input order. All searches
    share the same limit, threshold, and optional Qdrant payload filter.
    """

    if not vectors:
        return []
    if any(not vector for vector in vectors):
        raise ValueError("Query vectors cannot be empty.")
    if limit <= 0:
        raise ValueError("Limit must be greater than zero.")

    searches: list[dict[str, Any]] = []
    for vector in vectors:
        search: dict[str, Any] = {
            "vector": {"name": "dense", "vector": vector},
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        if score_threshold is not None:
            search["score_threshold"] = score_threshold
        if query_filter is not None:
            search["filter"] = query_filter
        searches.append(search)

    response = _request(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/search/batch",
        json={"searches": searches},
    )

    try:
        body = response.json()
    except ValueError as exc:
        logger.exception("Qdrant returned an invalid JSON response while batch searching points.")
        raise QdrantError("Qdrant returned an invalid response.") from exc

    result = body.get("result") if isinstance(body, dict) else None
    if (
        not isinstance(result, list)
        or len(result) != len(vectors)
        or not all(isinstance(points, list) and all(isinstance(point, dict) for point in points) for points in result)
    ):
        logger.error("Qdrant returned an invalid batch search result.")
        raise QdrantError("Qdrant returned an invalid batch search result.")

    return result