"""
src/services/tracing/__init__.py
--------------------------------
Low-level agentic auditing and tracing integration with LangSmith.
"""

from src.services.tracing.langsmith_tracer import (
    ContractReviewTracer,
    is_langsmith_enabled,
)

__all__ = ["ContractReviewTracer", "is_langsmith_enabled"]
