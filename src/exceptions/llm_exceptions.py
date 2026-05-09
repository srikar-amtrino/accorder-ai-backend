from src.exceptions.base_exception import AppException


class APIKeyNotConfigured(AppException):
    """Exception raised when no API key is configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DeploymentNotConfigured(AppException):
    """Exception raised when no deployment name is configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BaseURLNotConfigured(AppException):
    """Exception raised when no base URL is configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EmptyResponseError(AppException):
    """Exception raised when the LLM returns an empty response."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ResponseParsingError(AppException):
    """Exception raised when parsing the LLM response fails."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LLMModelError(AppException):
    """General exception for LLM model errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
