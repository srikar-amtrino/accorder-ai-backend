import asyncio
import json
import time
from typing import Any, Dict, Optional, Type, TypeVar

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

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type[ResponseModelT], session_id: str, system_message: Optional[str] = None, temperature: float = 0.0) -> ResponseModelT:

        prompt = self.render_prompt_template(
            prompt=prompt,
            context=context,
        )

        tool = {
            "name": response_model.__name__,
            "description": f"Submit a structured {response_model.__name__} response.",
            "input_schema": response_model.model_json_schema(),
        }

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10000,
            "temperature": temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
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

        model_response = json.loads(response_body)

        usage = model_response.get("usage", {})

        logger.info(
            "bedrock model generation complete",
            model_id=self.model_id,
            session_id=session_id,
            time_taken=time.time() - start_time,
            tokens_input=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

        tool_use_block = next(block for block in model_response["content"] if block["type"] == "tool_use")

        normalized_input = self._normalize_tool_response(tool_use_block["input"])

        return response_model.model_validate(normalized_input)

    async def generate_with_thinking(
        self,
        prompt: str,
        context: Dict[str, Any],
        response_model: Type[ResponseModelT],
        session_id: str,
        system_message: Optional[str] = None,
        thinking_budget_tokens: int = 8000,
    ) -> tuple:
        """Generate a structured response AND capture the model's extended thinking trace.

        Extended thinking is enabled via the ``anthropic_beta`` header
        ``interleaved-thinking-2025-05-14``.  The request uses temperature=1
        (required by the API when thinking is enabled) and a larger
        ``max_tokens`` budget to accommodate both thinking blocks and the
        structured tool-use output.

        Returns:
            (response_model_instance, thinking_text: str)
            ``thinking_text`` is the concatenation of all ``thinking`` content
            blocks returned by the model (the raw "scrap paper").  It is an
            empty string when the model returns no thinking blocks.
        """

        prompt = self.render_prompt_template(
            prompt=prompt,
            context=context,
        )

        tool = {
            "name": response_model.__name__,
            "description": f"Submit a structured {response_model.__name__} response.",
            "input_schema": response_model.model_json_schema(),
        }

        # Extended thinking requires temperature=1 and a max_tokens value that
        # is strictly greater than budget_tokens (we add 4096 for the output).
        max_tokens = thinking_budget_tokens + 4096

        enhanced_system = (
            (system_message or "You are a helpful assistant.")
            + f"\n\nIMPORTANT: You must return your structured output by calling the '{response_model.__name__}' tool."
        )

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": 1,
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            },
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "system": enhanced_system,
            "tools": [tool],
            "tool_choice": {
                "type": "auto",
            },
        }

        start_time = time.time()

        response = await asyncio.to_thread(
            self.client.invoke_model,
            modelId=self.model_id,
            body=json.dumps(native_request),
        )

        response_body = await asyncio.to_thread(response["body"].read)

        model_response = json.loads(response_body)

        usage = model_response.get("usage", {})

        logger.info(
            "bedrock model generation with thinking complete",
            model_id=self.model_id,
            session_id=session_id,
            time_taken=time.time() - start_time,
            tokens_input=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            thinking_budget=thinking_budget_tokens,
        )

        content_blocks = model_response.get("content", [])

        # Collect all thinking blocks (may be interleaved with text/tool_use)
        thinking_parts = [
            block.get("thinking", "")
            for block in content_blocks
            if block.get("type") == "thinking"
        ]
        thinking_text = "\n\n---\n\n".join(part for part in thinking_parts if part)

        tool_use_block = next(
            (block for block in content_blocks if block["type"] == "tool_use"),
            None,
        )
        if tool_use_block is None:
            # Fallback: check if tool call or JSON was emitted inside a text block
            text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            combined_text = "\n".join(text_blocks).strip()
            if "{" in combined_text:
                start = combined_text.find("{")
                end = combined_text.rfind("}") + 1
                try:
                    parsed = json.loads(combined_text[start:end])
                    normalized = self._normalize_tool_response(parsed)
                    return response_model.model_validate(normalized), thinking_text
                except Exception as ex:
                    logger.warning("Could not parse JSON from text block: %s", ex)

            raise LLMModelError(
                f"No tool_use block found in thinking-enabled response for {response_model.__name__}. "
                f"Content types returned: {[b.get('type') for b in content_blocks]}"
            )

        normalized_input = self._normalize_tool_response(tool_use_block["input"])

        return response_model.model_validate(normalized_input), thinking_text
