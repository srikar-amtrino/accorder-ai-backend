class AppException(Exception):
    """Base exception class for the application."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
