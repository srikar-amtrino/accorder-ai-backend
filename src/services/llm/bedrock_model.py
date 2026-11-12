import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

import boto3
import pystache
from botocore.config import Config
from pydantic import BaseModel

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.exceptions.llm_exceptions import (
    LLMModelError,
    LLMOutputTruncatedError,
)
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)

# The structured response type a caller asks for is the type it gets back.
ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


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

    # async def generate_stream(self, prompt: str, context: Dict[str, Any], session_id: str, system_message: Optional[str] = None) -> Any:
    #     """Yields raw text chunks as they arrive from Bedrock."""

    #     prompt = self.render_prompt_template(prompt=prompt, context=context)
    #     native_request = {
    #         "anthropic_version": "bedrock-2023-05-31",
    #         "max_tokens": 10000,
    #         "temperature": 0.5,
    #         "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    #         "system": system_message or "You are a helpful assistant.",
    #     }

    #     start_time = time.time()
    #     streaming_response = self.client.invoke_model_with_response_stream(
    #         modelId=self.model_id,
    #         body=json.dumps(native_request),
    #     )
    #     end_time = time.time()
    #     logger.info("invoked bedrock model with streaming response", model_id=self.model_id, session_id=session_id, time_taken=end_time - start_time)

    #     usage = streaming_response.get("usage", {})
    #     logger.info(
    #         "bedrock model generation complete",
    #         model_id=self.model_id,
    #         session_id=session_id,
    #         tokens_input=usage.get("input_tokens"),
    #         output_tokens=usage.get("output_tokens"),
    #     )

    #     for event in streaming_response["body"]:
    #         chunk = json.loads(event["chunk"]["bytes"])
    #         if chunk["type"] == "content_block_delta":
    #             text = chunk["delta"].get("text", "")
    #             if text:
    #                 yield text

    async def generate_stream(self, prompt: str, context: Dict[str, Any], session_id: str, temperature: float = 0.0, system_message: Optional[str] = None, max_tokens: int = 10000) -> Any:
        """Yields text chunks as they arrive from Bedrock.

        ``max_tokens`` bounds the streamed output (raise it for large responses such as
        document comparison).
        """

        prompt = self.render_prompt_template(
            prompt=prompt,
            context=context,
        )

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
            "system": system_message or "You are a helpful assistant.",
        }

        start_time = time.time()

        streaming_response = await asyncio.to_thread(
            self.client.invoke_model_with_response_stream,
            modelId=self.model_id,
            body=json.dumps(native_request),
        )

        body_iter = iter(streaming_response["body"])

        while True:
            event = await asyncio.to_thread(lambda: next(body_iter, None))

            if event is None:
                break

            chunk = json.loads(event["chunk"]["bytes"])

            if chunk.get("type") == "content_block_delta":
                text = chunk["delta"].get("text", "")

                if text:
                    yield text

                    # give control back to event loop
                    await asyncio.sleep(0)

        logger.info(
            "invoked bedrock model with streaming response",
            model_id=self.model_id,
            session_id=session_id,
            time_taken=time.time() - start_time,
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

    # async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type[BaseModel], session_id: str, system_message: Optional[str] = None) -> BaseModel:
    #     """Sends a generation request to Bedrock and returns the full response once complete."""

    #     prompt = self.render_prompt_template(prompt=prompt, context=context)

    #     tool = {
    #         "name": response_model.__name__,
    #         "description": f"Submit a structured {response_model.__name__} response.",
    #         "input_schema": response_model.model_json_schema(),
    #     }

    #     native_request = {
    #         "anthropic_version": "bedrock-2023-05-31",
    #         "max_tokens": 10000,
    #         "temperature": 0.5,
    #         "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    #         "system": system_message or "You are a helpful assistant.",
    #         "tools": [tool],
    #         "tool_choice": {"type": "tool", "name": response_model.__name__},
    #     }

    #     start_time = time.time()
    #     response = self.client.invoke_model(
    #         modelId=self.model_id,
    #         body=json.dumps(native_request),
    #     )
    #     end_time = time.time()

    #     logger.info("invoked bedrock model with generate", model_id=self.model_id, session_id=session_id, time_taken=end_time - start_time)

    #     model_response = json.loads(response["body"].read())
    #     usage = model_response.get("usage", {})
    #     logger.info(
    #         "bedrock model generation complete",
    #         model_id=self.model_id,
    #         session_id=session_id,
    #         tokens_input=usage.get("input_tokens"),
    #         output_tokens=usage.get("output_tokens"),
    #     )
    #     tool_use_block = next(block for block in model_response["content"] if block["type"] == "tool_use")
    #     normalized_input = self._normalize_tool_response(tool_use_block["input"])

    #     return cast(response_model, response_model.model_validate(normalized_input))  # type: ignore

    async def generate(
        self,
        prompt: str,
        context: Dict[str, Any],
        response_model: Type[ResponseModelT],
        session_id: str,
        system_message: Optional[str] = None,
        temperature: float = 0.0,
        *,
        max_tokens: int = 10000,
        cached_prefix: Optional[str] = None,
        fail_on_truncation: bool = False,
    ) -> ResponseModelT:
        """Send a schema-enforced generation request and return the validated response.

        cached_prefix is text placed ahead of the rendered prompt and marked
        for Bedrock prompt caching: calls that share the same tool, system
        message and prefix read it from the cache instead of paying for it
        again. With fail_on_truncation, a response cut off at max_tokens raises
        LLMOutputTruncatedError instead of being validated as if it were whole.
        """

        rendered = self.render_prompt_template(prompt=prompt, context=context)
        model_response = await self._invoke_structured(rendered, response_model, session_id, system_message, temperature, max_tokens, cached_prefix)

        if model_response.get("stop_reason") == "max_tokens":
            logger.warning("bedrock model stopped at its output token limit; the response is incomplete", model_id=self.model_id, session_id=session_id, max_tokens=max_tokens)
            if fail_on_truncation:
                raise LLMOutputTruncatedError(f"The model stopped at its {max_tokens}-token output limit before finishing its {response_model.__name__} response.")

        tool_use_block = next(block for block in model_response["content"] if block["type"] == "tool_use")

        normalized_input = self._normalize_tool_response(tool_use_block["input"])

        return response_model.model_validate(normalized_input)

    async def warm_prompt_cache(self, cached_prefix: str, response_model: Type[BaseModel], session_id: str, system_message: Optional[str] = None) -> None:
        """Write cached_prefix to the Bedrock prompt cache without generating a response.

        Calls that start together all miss the cache, because an entry exists
        only once a first response has begun. This sends the same tool, system
        message and prefix a generate() call would, limited to one output
        token, so the entry is written in a second or two and the calls that
        follow read it.
        """

        await self._invoke_structured("", response_model, session_id, system_message, 0.0, 1, cached_prefix)

    async def _invoke_structured(
        self, rendered_prompt: str, response_model: Type[BaseModel], session_id: str, system_message: Optional[str], temperature: float, max_tokens: int, cached_prefix: Optional[str]
    ) -> Dict[str, Any]:
        """Invoke the model with response_model as a forced tool and return the raw response body."""

        tool = {
            "name": response_model.__name__,
            "description": f"Submit a structured {response_model.__name__} response.",
            "input_schema": response_model.model_json_schema(),
        }

        content: List[Dict[str, Any]] = [{"type": "text", "text": rendered_prompt or "-"}]
        if cached_prefix:
            content.insert(0, {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}})

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "system": system_message or "You are a helpful assistant.",
            "tools": [tool],
            "tool_choice": {
                "type": "tool",
                "name": response_model.__name__,
            },
        }

        start_time = time.time()

        response = await asyncio.to_thread(
            self.client.invoke_model,
            modelId=self.model_id,
            body=json.dumps(native_request),
        )

        response_body = await asyncio.to_thread(response["body"].read)

        model_response: Dict[str, Any] = json.loads(response_body)

        usage = model_response.get("usage", {})

        logger.info(
            "bedrock model generation complete",
            model_id=self.model_id,
            session_id=session_id,
            time_taken=time.time() - start_time,
            tokens_input=usage.get("input_tokens"),
            tokens_cache_read=usage.get("cache_read_input_tokens"),
            tokens_cache_write=usage.get("cache_creation_input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

        return model_response
