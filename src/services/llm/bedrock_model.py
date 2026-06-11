import asyncio
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

    async def generate_stream(self, prompt: str, context: Dict[str, Any], session_id: str, system_message: Optional[str] = None, cache_system: bool = False, max_tokens: int = 10000) -> Any:
        """Yields raw text chunks as they arrive from Bedrock."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)

        if cache_system and system_message:
            system_field: Any = [{"type": "text", "text": system_message, "cache_control": {"type": "ephemeral"}}]
        else:
            system_field = system_message or "You are a helpful assistant."

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": 0.5,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "system": system_field,
        }

        start_time = time.time()
        streaming_response = await asyncio.to_thread(
            self.client.invoke_model_with_response_stream,
            modelId=self.model_id,
            body=json.dumps(native_request),
        )

        # Usage (including cache tokens) is delivered inside the stream events, not on the
        # top-level response: message_start carries input + cache tokens, message_delta the output.
        usage: Dict[str, Any] = {}
        first_chunk_time = None

        body_iter = iter(streaming_response["body"])
        while True:
            event = await asyncio.to_thread(lambda: next(body_iter, None))
            if event is None:
                break

            chunk = json.loads(event["chunk"]["bytes"])
            chunk_type = chunk.get("type")
            if chunk_type == "message_start":
                usage.update(chunk.get("message", {}).get("usage", {}))
            elif chunk_type == "content_block_delta":
                text = chunk["delta"].get("text", "")
                if text:
                    if first_chunk_time is None:
                        first_chunk_time = time.time()
                    yield text
                    # give control back to event loop
                    await asyncio.sleep(0)
            elif chunk_type == "message_delta":
                usage.update(chunk.get("usage", {}))

        end_time = time.time()
        logger.info(
            "bedrock streaming generation complete",
            model_id=self.model_id,
            session_id=session_id,
            time_to_first_chunk=(first_chunk_time - start_time) if first_chunk_time else None,
            total_time=end_time - start_time,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
        )

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

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type[BaseModel], session_id: str, system_message: Optional[str] = None, cache_system: bool = False, max_tokens: int = 10000) -> BaseModel:
        """Sends a generation request to Bedrock and returns the full response once complete."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)

        tool = {
            "name": response_model.__name__,
            "description": f"Submit a structured {response_model.__name__} response.",
            "input_schema": response_model.model_json_schema(),
        }

        # When caching is requested, send the system prompt as a content block with an
        # ephemeral cache breakpoint so a repeated system prompt is billed at the cache
        # read rate on subsequent calls within the cache window.
        if cache_system and system_message:
            system_field: Any = [{"type": "text", "text": system_message, "cache_control": {"type": "ephemeral"}}]
        else:
            system_field = system_message or "You are a helpful assistant."

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": 0.5,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "system": system_field,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": response_model.__name__},
        }

        start_time = time.time()
        response = await asyncio.to_thread(
            self.client.invoke_model,
            modelId=self.model_id,
            body=json.dumps(native_request),
        )
        end_time = time.time()

        logger.info("invoked bedrock model with generate", model_id=self.model_id, session_id=session_id, time_taken=end_time - start_time)

        response_body = await asyncio.to_thread(response["body"].read)
        model_response = json.loads(response_body)
        usage = model_response.get("usage", {})
        logger.info(
            "bedrock model generation complete",
            model_id=self.model_id,
            session_id=session_id,
            time_taken=end_time - start_time,
            tokens_input=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
        )
        tool_use_block = next(block for block in model_response["content"] if block["type"] == "tool_use")
        normalized_input = self._normalize_tool_response(tool_use_block["input"])

        return cast(response_model, response_model.model_validate(normalized_input))  # type: ignore
