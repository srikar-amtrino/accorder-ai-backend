import hashlib
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """Individual document chunk with metadata."""

    chunk_id: Union[str, None] = Field(None, description="Unique identifier for the chunk")
    document_id: Union[str, None] = Field(None, description="Identifier of the source document")
    chunk_index: int = Field(..., description="Index of the chunk within the document.")
    content: str = Field(..., description="Text content of the chunk.")

    # Embedding Information
    embedding_vector: Optional[List[float]] = Field(None, description="Vector representation of the chunk.")
    embedding_model: Optional[str] = Field(None, description="Model used to generate the embeddings.")

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata for the chunk.")
    created_at: Optional[str] = Field(None, description="Timestamp when the chunk was created.")

    def get_content_hash(self) -> str:
        """Generate hash content for duplication checks."""

        return hashlib.sha256(self.content.encode()).hexdigest()


class ParseResult(BaseModel):
    """Schema for the result of a parsing operation in the registry service."""

    success: bool = Field(..., description="Indicates if the parsing was successful")
    chunks: List[Chunk] = Field(default_factory=list, description="List of parsed data chunks")
    metadata: dict = Field(default_factory=dict, description="Additional metadata about the parsing operation(if any)")
    error_message: Union[str, None] = Field(None, description="Error message if the parsing has any failed operations.")
    processing_time: float = Field(..., description="Time taken to process the parsing operation in seconds.")
