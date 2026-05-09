from src.exceptions.base_exception import AppException


class FAISSDimensionMismatchException(AppException):
    """Exception raised when the embedding dimension does not match the FAISS index dimension."""

    def __init__(self, expected_dim: int, actual_dim: int) -> None:
        message = f"Expected embedding dimension {expected_dim}, but got {actual_dim}."
        super().__init__(message)


class FAISSEmptyEmbeddingException(AppException):
    """Exception raised when trying to index an empty embedding vector."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FAISSUnableToIndexException(AppException):
    """Exception raised when FAISS is unable to index the provided embeddings."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FAISSEmptyQueryException(AppException):
    """Exception raised when trying to search with an empty query embedding."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FAISSUnableToSearchException(AppException):
    """Exception raised when FAISS is unable to perform the search operation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
