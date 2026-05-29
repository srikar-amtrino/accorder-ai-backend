import os
from functools import lru_cache
from typing import List, Union

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration settings."""

    # API settings
    api_host: str = Field(default="0.0.0.0", description="Interface the FastAPI server binds to. '0.0.0.0' listens on all interfaces (needed for EC2/external access); 'localhost' for local-only.")
    api_port: int = Field(default=8000, description="API port number")
    debug: bool = Field(default=False, description="Enable or disable debug mode")

    # CORS settings — comma-separated origins, or "*" to allow all (dev only).
    # Word task-pane add-ins load from the manifest's SourceLocation host, so list that origin here.
    cors_allow_origins: str = Field(default="*", description="Comma-separated allowed origins for CORS, or '*' to allow all.")

    @property
    def cors_origins_list(self) -> List[str]:
        raw = (self.cors_allow_origins or "").strip()
        if raw == "*" or raw == "":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    # Chunking settings
    chunk_size: int = Field(default=1000, description="Size of each text chunk")
    chunk_overlap: int = Field(default=200, description="Overlap size between text chunks")

    # Embedding settings (HuggingFace MiniLM — runs locally, no API key needed)
    huggingface_minilm_embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="HuggingFace Sentence-Transformers embedding model (384 dimensions).",
    )

    # Auth settings
    JWT_SECRET_KEY: str = Field(default="your-secret-key", description="Secret key for encoding and decoding JWT tokens. In production, use a secure, random value and keep it secret.")
    JWT_ALGORITHM: str = Field(default="HS256", description="Algorithm used for JWT encoding and decoding.")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, description="Expiration time for JWT access tokens in minutes.")

    # AWS Bedrock settings
    aws_region: str = Field(default="us-west-1", description="AWS region for the Bedrock runtime client.")
    bedrock_model_id: Union[str, None] = Field(default=None, description="Bedrock model id or inference profile ARN for Anthropic Claude.")

    # Storage paths
    logs_directory: str = Field(default="./logs", description="Directory for application logs")

    # Application Auth
    token_url: str = Field(default=..., description="")
    client_id: str = Field(default=..., description="")
    client_secret: str = Field(default=..., description="")
    aws_cognito_region: str = Field(default="us-west-1", description="")
    aws_cognito_user_pool_id: str = Field(default=..., description="")
    aws_cognito_issuer: str = Field(default=..., description="")
    aws_cognito_jwks_url: str = Field(default=..., description="")

    aws_access_key_id: Union[str, None] = Field(default=None, description="AWS Access Key ID for Bedrock authentication. Optional if using IAM roles or other credential providers.")
    aws_secret_access_key: Union[str, None] = Field(default=None, description="AWS Secret Access Key for Bedrock authentication. Optional if using IAM roles or other credential providers.")

    # Session management settings
    session_ttl_minutes: int = Field(default=120, description="Session TTL in minutes (default: 2 hours).")
    session_cleanup_interval_minutes: float = Field(default=10.0, description="How often to check for expired sessions, in minutes.")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings instance."""

    return Settings()


def ensure_directories() -> None:
    """Ensure directories exists."""

    settings = get_settings()
    os.makedirs(settings.logs_directory, exist_ok=True)


ensure_directories()
