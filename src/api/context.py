"""
Context management for request-scoped data like session_id and document_id.

This module uses Python's contextvars to store request context that automatically
propagates through async function calls without needing to pass parameters explicitly.
"""

from contextvars import ContextVar
from typing import Optional

# Context variables for storing request-scoped data
session_id_var: ContextVar[Optional[str]] = ContextVar("session_id", default=None)
document_id_var: ContextVar[Optional[str]] = ContextVar("document_id", default=None)
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def set_session_id(session_id: Optional[str]) -> None:
    """Set the session_id in the current context."""
    session_id_var.set(session_id)


def get_session_id() -> Optional[str]:
    """Get the session_id from the current context."""
    return session_id_var.get()


def set_document_id(document_id: Optional[str]) -> None:
    """Set the document_id in the current context."""
    document_id_var.set(document_id)


def get_document_id() -> Optional[str]:
    """Get the document_id from the current context."""
    return document_id_var.get()


def set_request_id(request_id: Optional[str]) -> None:
    """Set the request_id in the current context."""
    request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    """Get the request_id from the current context."""
    return request_id_var.get()


def clear_context() -> None:
    """Clear all context variables."""
    session_id_var.set(None)
    document_id_var.set(None)
    request_id_var.set(None)
