"""
Lightweight psycopg2 connection factory for the Accorder AI backend.

Reads DATABASE_URL from settings/env and returns a plain psycopg2 connection.
No connection pooling by default — each call opens a new connection and the
caller is responsible for closing it (use as a context manager).

Usage:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Generator
from urllib.parse import parse_qs, unquote, urlparse

from src.config.logging import get_logger

logger = get_logger(__name__)

# Cache the parsed DSN so we don't reparse on every call
_CONNECT_KWARGS: dict | None = None


def _parse_database_url(url: str) -> dict:
    """Parse a postgresql+psycopg2://... URL into psycopg2.connect() kwargs."""
    # Strip SQLAlchemy dialect prefix if present
    url = re.sub(r"^postgresql\+psycopg2://", "postgresql://", url)
    parsed = urlparse(url)

    kwargs: dict = {}

    if parsed.hostname:
        kwargs["host"] = parsed.hostname
    if parsed.port:
        kwargs["port"] = parsed.port
    if parsed.username:
        kwargs["user"] = unquote(parsed.username)
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    if parsed.path and parsed.path != "/":
        kwargs["dbname"] = parsed.path.lstrip("/")

    # Parse query string for ssl params
    qs = parse_qs(parsed.query)
    sslmode = qs.get("sslmode", [None])[0]
    sslrootcert = qs.get("sslrootcert", [None])[0]

    if sslmode:
        kwargs["sslmode"] = sslmode
    if sslrootcert:
        # Percent-decode any %2F → / (Windows paths)
        kwargs["sslrootcert"] = unquote(sslrootcert)

    return kwargs


def _get_connect_kwargs() -> dict:
    global _CONNECT_KWARGS
    if _CONNECT_KWARGS is not None:
        return _CONNECT_KWARGS

    # Prefer settings, fall back to raw env var
    try:
        from src.config.settings import get_settings  # noqa: PLC0415
        url = get_settings().database_url
    except Exception:
        url = None

    if not url:
        url = os.environ.get("DATABASE_URL", "")

    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to your .env file or environment."
        )

    _CONNECT_KWARGS = _parse_database_url(url)
    logger.debug("DB connection target: host=%s db=%s", _CONNECT_KWARGS.get("host"), _CONNECT_KWARGS.get("dbname"))
    return _CONNECT_KWARGS


@contextmanager
def get_db_connection() -> Generator:
    """Yield an open psycopg2 connection; always closes it on exit."""
    import psycopg2  # noqa: PLC0415 — optional dep, imported lazily

    kwargs = _get_connect_kwargs()
    conn = psycopg2.connect(**kwargs)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reset_connection_cache() -> None:
    """Force re-parse of DATABASE_URL on next call (useful in tests)."""
    global _CONNECT_KWARGS
    _CONNECT_KWARGS = None

