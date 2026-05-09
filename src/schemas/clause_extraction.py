from typing import List

from pydantic import BaseModel, Field


class SubClause(BaseModel):
    """Represents a sub-clause within a clause."""

    number: str = Field(..., description="The number or identifier of the sub-clause")
    title: str = Field("", description="The title of the sub-clause")
    content: str = Field(..., description="The content of the sub-clause")


class Clause(BaseModel):
    """Represents a clause in a legal document."""

    number: str = Field(..., description="The number or identifier of the clause")
    title: str = Field(..., description="The title of the clause")
    content: str = Field(..., description="The content of the clause")
    sub_clauses: List[SubClause] = Field(default_factory=list, description="List of sub-clauses within this clause")


class ClauseExtractionResult(BaseModel):
    """Result of clause extraction from a document."""

    document: str = Field(..., description="The name of the document processed")
    clauses: List[Clause] = Field(default_factory=list, description="List of extracted clauses")
