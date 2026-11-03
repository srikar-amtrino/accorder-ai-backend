import asyncio
import logging
import os
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when the embedding service fails or returns an unusable response."""


class EmbeddingClient:
    """Call the remote embedding service without blocking the event loop."""

    _server_lock = threading.Lock()

    def __init__(
        self,
        url: str | None = None,
        timeout: float = 120.0,
        batch_size: int = 4,
        max_retries: int = 3,
    ) -> None:
        self._url = url or os.getenv(
            "EMBEDDING_SERVICE_URL",
            "http://52.53.197.54:8000/embed",
        )
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._max_retries = max(1, max_retries)

    def _post(self, texts: list[str]) -> list[list[float]]:
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            status_code = None

            try:
                with self._server_lock:
                    response = requests.post(
                        self._url,
                        json={"texts": texts},
                        headers={"Accept": "*/*"},
                        timeout=self._timeout,
                    )

                status_code = response.status_code
                response.raise_for_status()
                return self._parse(response.json(), len(texts))

            except requests.Timeout as exc:
                raise EmbeddingError(
                    f"Embedding service timed out after {self._timeout}s "
                    f"for a batch of {len(texts)}."
                ) from exc

            except (ValueError, EmbeddingError) as exc:
                raise EmbeddingError(
                    f"Embedding service returned an unusable response: {exc}"
                ) from exc

            except requests.RequestException as exc:
                last_error = exc
                status_code = status_code or getattr(
                    getattr(exc, "response", None),
                    "status_code",
                    None,
                )
                logger.warning(
                    "Embedding call failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": self._max_retries,
                        "batch_size": len(texts),
                        "http_status": status_code,
                        "error": str(exc),
                    },
                )

                if status_code is not None and status_code < 500:
                    break

                if attempt < self._max_retries:
                    time.sleep(min(2**attempt, 10))

        raise EmbeddingError(f"Embedding service failed: {last_error}") from last_error

    @staticmethod
    def _parse(payload: Any, expected: int) -> list[list[float]]:
        vectors = payload

        if isinstance(payload, dict):
            for key in ("embeddings", "vectors", "data"):
                if key in payload:
                    vectors = payload[key]
                    break

        if not isinstance(vectors, list) or len(vectors) != expected:
            raise EmbeddingError(
                "Embedding response does not match the number of texts sent."
            )

        parsed: list[list[float]] = []
        for vector in vectors:
            if isinstance(vector, dict):
                vector = vector.get("embedding")

            if (
                not isinstance(vector, list)
                or not vector
                or not all(isinstance(value, (int, float)) for value in vector)
            ):
                raise EmbeddingError(
                    "Embedding response contains an invalid vector."
                )

            parsed.append([float(value) for value in vector])

        return parsed

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, preserving order."""

        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding input contains empty or non-string text.")

        started = time.perf_counter()
        vectors: list[list[float]] = []

        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await asyncio.to_thread(self._post, batch))

        logger.info(
            "Texts embedded",
            extra={
                "count": len(texts),
                "dimension": len(vectors[0]) if vectors else 0,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return vectors


embedding_client = EmbeddingClient()
