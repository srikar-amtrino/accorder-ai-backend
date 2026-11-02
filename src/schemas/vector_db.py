from pydantic import BaseModel, Field, field_validator


class ClauseSimilarityRequest(BaseModel):
    """Request a similarity search for a contract clause."""

    clause_text: str = Field(
        min_length=1,
        max_length=10000,
        description="Clause text used to find similar records.",
    )
    limit: int = Field(default=5, ge=1, le=100, description="Maximum number of matches.")
    score_threshold: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Optional minimum cosine similarity score.",
    )

    @field_validator("clause_text")
    @classmethod
    def validate_clause_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Clause text cannot be blank.")
        return value
