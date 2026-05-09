from src.exceptions.base_exception import AppException


class DocxCleaningException(AppException):
    """Exception raised for errors during DOCX document cleaning."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocxMetadataExtractionException(AppException):
    """Exception raised for errors during DOCX metadata extraction."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocxParagraphExtractionException(AppException):
    """Exception raised for errors during DOCX paragraph extraction."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocxTableExtractionException(AppException):
    """Exception raised for errors during DOCX table extraction."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EmptyTextException(AppException):
    """Exception raised when the extracted text from a document is empty."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ParserAlreadyRegistered(AppException):
    """Exception raised when trying to register a parser that is already registered."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
