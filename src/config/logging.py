import logging
import logging.config
import os
from datetime import datetime
from typing import Any, Dict, cast

from src.config.settings import get_settings


class ContextualFilter(logging.Filter):
    """Filter that adds context variables (session_id, document_id, request_id) to log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add context variables to the log record."""
        try:
            from src.api.context import get_session_id

            record.session_id = get_session_id() or "-"
        except (ImportError, RuntimeError):
            # Import error can happen at startup, runtime error if context not initialized
            record.session_id = "-"

        return True


def setup_logging() -> None:
    """Set up application logging configuration."""
    try:
        import io
        import sys

        stdout = cast(io.TextIOWrapper, sys.stdout)
        stderr = cast(io.TextIOWrapper, sys.stderr)

        stdout.reconfigure(encoding="utf-8", errors="replace")
        stderr.reconfigure(encoding="utf-8", errors="replace")

    except Exception:
        # reconfigure is available on Python 3.7+; if it fails just ignore it.
        pass

    settings = get_settings()

    # Ensure logs directory exists
    os.makedirs(settings.logs_directory, exist_ok=True)

    # Create log filename with timestamp
    log_filename = os.path.join(
        settings.logs_directory,
        f"AI_Contract_Review_{datetime.now().strftime('%Y%m%d')}.log",
    )

    logging_config: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "contextual": {
                "()": ContextualFilter,
            }
        },
        "formatters": {
            "detailed": {"format": ("%(asctime)s - %(name)s - %(levelname)s - " "[session:%(session_id)s] - " "%(filename)s:%(lineno)d - %(funcName)s - %(message)s")},
            "simple": {"format": "%(asctime)s - %(levelname)s - [%(session_id)s] - %(message)s"},
            "json": {
                "format": (
                    '{"timestamp": "%(asctime)s", "session_id": "%(session_id)s", '
                    '"document_id": "%(document_id)s", "request_id": "%(request_id)s", '
                    '"logger": "%(name)s", "level": "%(levelname)s", "file": "%(filename)s", '
                    '"line": %(lineno)d, "function": "%(funcName)s", '
                    '"message": "%(message)s"}'
                )
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "simple",
                "stream": "ext://sys.stdout",
                "filters": ["contextual"],
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "detailed",
                "filename": log_filename,
                "encoding": "utf-8",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "filters": ["contextual"],
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "json",
                "filename": os.path.join(settings.logs_directory, "errors.log"),
                "encoding": "utf-8",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "filters": ["contextual"],
            },
        },
        "loggers": {
            "contract_review": {
                "level": "DEBUG" if settings.debug else "INFO",
                "handlers": ["console", "file", "error_file"],
                "propagate": False,
            },
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console", "file"],
                "propagate": False,
            },
            # "chromadb": {"level": "WARNING", "handlers": ["file"], "propagate": False},
        },
        "root": {"level": "INFO", "handlers": ["console", "file"]},
    }

    logging.config.dictConfig(logging_config)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the specified name."""
    return logging.getLogger(f"AI_Contract.{name}")


class Logger:
    """Mixin class to provide logging functionality."""

    @property
    def logger(self) -> logging.Logger:
        """Get logger for this class."""
        return get_logger(self.__class__.__name__)
