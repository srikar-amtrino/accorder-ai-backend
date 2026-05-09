from src.exceptions.base_exception import AppException


class ParserNotFound(AppException):
    """Exception raised when no parser is found."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
