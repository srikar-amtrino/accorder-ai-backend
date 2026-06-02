import json
import time
from typing import Any, Dict, Optional, Type, cast

import boto3
import pystache
from botocore.config import Config
from pydantic import BaseModel

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.exceptions.llm_exceptions import (
    LLMModelError,
)
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)


class BedrockModel(BaseLLMModel):
    """The base llm wrapper for bedrock models."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()

        self.model_id = self.settings.bedrock_model_id
        if not self.model_id:
            raise LLMModelError("Bedrock model id is not configured. Set 'BEDROCK_MODEL_ID' in the environment.")

        self.region = self.settings.aws_region
        if not self.region:
            raise LLMModelError("AWS region is not configured. Set 'AWS_REGION' in the environment.")

        if self.settings.env == "development":
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                aws_access_key_id=self.settings.aws_access_key_id,
                aws_secret_access_key=self.settings.aws_secret_access_key,
                config=Config(
                    read_timeout=300,
                    connect_timeout=10,
                    retries={"max_attempts": 1},
                ),
            )
            logger.info("initialized bedrock client with explicit credentials for development environment", model=self.model_id, env=self.settings.env)
        else:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    read_timeout=300,
                    connect_timeout=10,
                    retries={"max_attempts": 1},
                ),
            )
            logger.info("initialized bedrock client with default credentials for production environment", model=self.model_id, env=self.settings.env)

    def render_prompt_template(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Mustache prompt template render."""

        return pystache.render(template=prompt, context=context)

    async def generate_stream(self, prompt: str, context: Dict[str, Any], session_id: str, system_message: Optional[str] = None) -> Any:
        """Yields raw text chunks as they arrive from Bedrock."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10000,
            "temperature": 0.5,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "system": system_message or "You are a helpful assistant.",
        }

        start_time = time.time()
        streaming_response = self.client.invoke_model_with_response_stream(
            modelId=self.model_id,
            body=json.dumps(native_request),
        )
        end_time = time.time()
        logger.info("invoked bedrock model with streaming response", model_id=self.model_id, session_id=session_id, time_taken=end_time - start_time)

        for event in streaming_response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            if chunk["type"] == "content_block_delta":
                text = chunk["delta"].get("text", "")
                if text:
                    yield text

    def _normalize_tool_response(self, value: Any) -> Any:
        if isinstance(value, str):
            raw_value = value.strip()
            if raw_value.startswith("[") or raw_value.startswith("{"):
                try:
                    return self._normalize_tool_response(json.loads(raw_value))
                except json.JSONDecodeError:
                    return value
            return value

        if isinstance(value, dict):
            return {key: self._normalize_tool_response(val) for key, val in value.items()}

        if isinstance(value, list):
            return [self._normalize_tool_response(item) for item in value]

        return value

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type[BaseModel], session_id: str, system_message: Optional[str] = None) -> BaseModel:
        """Sends a generation request to Bedrock and returns the full response once complete."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)

        tool = {
            "name": response_model.__name__,
            "description": f"Submit a structured {response_model.__name__} response.",
            "input_schema": response_model.model_json_schema(),
        }

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10000,
            "temperature": 0.5,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "system": system_message or "You are a helpful assistant.",
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": response_model.__name__},
        }

        start_time = time.time()
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(native_request),
        )
        end_time = time.time()

        logger.info("invoked bedrock model with generate", model_id=self.model_id, session_id=session_id, time_taken=end_time - start_time)

        model_response = json.loads(response["body"].read())
        tool_use_block = next(block for block in model_response["content"] if block["type"] == "tool_use")
        normalized_input = self._normalize_tool_response(tool_use_block["input"])

        return cast(response_model, response_model.model_validate(normalized_input))  # type: ignore
