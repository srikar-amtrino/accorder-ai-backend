import asyncio
from typing import Any, Dict

import requests  # type: ignore

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.services.vector_store.embeddings.base_embedding_service import (
    BaseEmbeddingService,
)

logger = get_logger(__name__)
settings = get_settings()


class AmazonTitanEmbeddingService(BaseEmbeddingService):
    """Amazon Titan Embedding Service."""

    def __init__(self) -> None:

        if not settings.aws_bedrock_api_key:
            raise ValueError("AWS Bedrock API key is not configured. Set 'AWS_BEDROCK_API_KEY' in the environment variables.")
        if not settings.aws_bedrock_region:
            raise ValueError("AWS_BEDROCK_REGION is not set in the environment variables.")
        if not settings.aws_bedrock_embed_model_id:
            raise ValueError("AWS_BEDROCK_EMBED_MODEL_ID is not set in the environment variables.")

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.aws_bedrock_api_key}",
        }
        self.base_url = f"https://bedrock-runtime.{settings.aws_bedrock_region}.amazonaws.com"
        self.model_id = settings.aws_bedrock_embed_model_id

        logger.info("Initialized Amazon Titan Embedding Service", region=settings.aws_bedrock_region, model_id=self.model_id)

    async def generate_embeddings(self, text: str, session_id: str, task: str | None = None) -> list[float]:
        """Generate embeddings for the given text using Amazon Titan."""

        url = f"{self.base_url}/model/{self.model_id}/invoke"
        payload: Dict[str, Any] = {"inputText": text}

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, headers=self.headers, timeout=30),
            )
            response.raise_for_status()
            logger.info("Successfully fetched embedding from AWS Bedrock.", status_code=response.status_code, model=settings.aws_bedrock_embed_model_id, session_id=session_id, task=task)
            return response.json().get("embedding", [])  # type: ignore

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching embedding: {e}")
            return []
