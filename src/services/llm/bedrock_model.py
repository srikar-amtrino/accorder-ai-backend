import asyncio
import json
from typing import Any, Dict, List, Optional, Type, Union

import boto3
import pystache
from botocore.config import Config
from pydantic import ValidationError

from src.config.logging import Logger
from src.config.settings import get_settings
from src.exceptions.llm_exceptions import (
    EmptyResponseError,
    LLMModelError,
    ResponseParsingError,
)
from src.services.llm.base_model import BaseLLMModel


class BedrockModel(BaseLLMModel, Logger):
    """AWS Bedrock Claude model for generating responses.

    Uses streaming (`invoke_model_with_response_stream`) for every call so the
    HTTP read timeout applies between chunks rather than against total
    generation time. Long-output prompts (e.g. key_information_prompt) reliably
    complete this way, while the final response stays byte-identical to a
    non-streaming call.

    AWS credentials are not handled here. They come from the standard
    credential chain: IAM role on EC2, or env vars / ~/.aws/credentials
    locally.
    """

    def __init__(self) -> None:
        """Initialize the Bedrock runtime client."""

        super().__init__()
        self.settings = get_settings()

        self.model_id: Union[str, None] = getattr(self.settings, "bedrock_model_id", None)
        if not self.model_id:
            raise LLMModelError("Bedrock model id is not configured. Set 'BEDROCK_MODEL_ID' in the environment.")

        self.region: Union[str, None] = getattr(self.settings, "aws_region", None)
        if not self.region:
            raise LLMModelError("AWS region is not configured. Set 'AWS_REGION' in the environment.")

        # read_timeout = 300s between chunks (chunks arrive every ~50ms during generation, so this is generous).
        # max_attempts = 1 to avoid restarting a long generation on transient hiccups; the caller decides retry policy.
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

    def render_prompt_template(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Mustache prompt template render function."""

        return pystache.render(template=prompt, context=context)

    def _stream_invoke(self, body: Dict[str, Any]) -> Any:
        """Send an invoke_model_with_response_stream request and return the body iterator."""

        response = self.client.invoke_model_with_response_stream(
            modelId=self.model_id,
            body=json.dumps(body),
        )
        return response["body"]

    def _iter_events(self, stream: Any) -> Any:
        """Yield parsed event dicts from a Bedrock event stream.

        Emits INFO-level progress logs so a `tail -f` of the log file shows
        the stream progressing in real time — start, every-25th delta with a
        text snippet, and complete.
        """

        chunk_count = 0
        delta_count = 0
        for event in stream:
            chunk_bytes = event.get("chunk", {}).get("bytes")
            if not chunk_bytes:
                continue
            chunk_count += 1
            parsed = json.loads(chunk_bytes)

            event_type = parsed.get("type")
            if event_type == "message_start":
                self.logger.info("[stream] started — first chunk received from Bedrock")
            elif event_type == "content_block_delta":
                delta_count += 1
                if delta_count % 25 == 0:
                    delta = parsed.get("delta", {})
                    snippet = (delta.get("text") or delta.get("partial_json") or "")[:40].replace("\n", " ")
                    self.logger.info(f"[stream] chunk #{chunk_count} (delta #{delta_count}): {snippet!r}")
            elif event_type == "message_stop":
                self.logger.info(f"[stream] complete — {chunk_count} chunks, {delta_count} text deltas")

            yield parsed

    def _collect_tool_use(self, body: Dict[str, Any]) -> Any:
        """Blocking: invoke + drain the stream for a tool-use (JSON) call.

        Runs the synchronous boto3 streaming loop. Callers MUST invoke this via
        ``asyncio.to_thread`` so it does not block the event loop — single-worker
        uvicorn would otherwise serialize every concurrent agent call, defeating
        the asyncio.gather/semaphore fan-outs in the comparison/playbook/review tools.
        """
        tool_name: Optional[str] = None
        tool_input_chunks: List[str] = []
        usage: Dict[str, int] = {}
        for chunk in self._iter_events(self._stream_invoke(body)):
            event_type = chunk.get("type")
            if event_type == "message_start":
                usage.update(chunk.get("message", {}).get("usage", {}))
            elif event_type == "message_delta":
                usage.update(chunk.get("usage", {}))
            elif event_type == "content_block_start":
                block = chunk.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_name = block.get("name")
            elif event_type == "content_block_delta":
                delta = chunk.get("delta", {})
                if delta.get("type") == "input_json_delta":
                    tool_input_chunks.append(delta.get("partial_json", ""))
        return tool_name, "".join(tool_input_chunks), usage

    def _collect_text(self, body: Dict[str, Any]) -> Any:
        """Blocking: invoke + drain the stream for a text/markdown call. Call via asyncio.to_thread."""
        text_chunks: List[str] = []
        usage: Dict[str, int] = {}
        for chunk in self._iter_events(self._stream_invoke(body)):
            event_type = chunk.get("type")
            if event_type == "message_start":
                usage.update(chunk.get("message", {}).get("usage", {}))
            elif event_type == "message_delta":
                usage.update(chunk.get("usage", {}))
            elif event_type == "content_block_delta":
                delta = chunk.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_chunks.append(delta.get("text", ""))
        return "".join(text_chunks), usage

    async def stream(self, prompt: str, context: Dict[str, Any], system_message: Optional[str] = None, max_tokens: int = 4096) -> Any:
        """Stream text deltas from Claude as they arrive."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        try:
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "system": system_message or "Extract the information and return valid JSON.",
            }

            for chunk in self._iter_events(self._stream_invoke(body)):
                if chunk.get("type") != "content_block_delta":
                    continue
                delta = chunk.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")

        except Exception as e:
            self.logger.error(f"An error occurred while streaming response from the LLM model: {str(e)}")
            raise LLMModelError("An error occurred while streaming response from the LLM model.") from e

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Union[Type, None], mode: str = "JSON", system_message: Optional[str] = None, cache_system: bool = False, max_tokens: int = 4096) -> Any:
        """Generate a response from Claude on Bedrock.

        JSON mode forces a tool-use call against the Pydantic response_model
        schema and returns a validated instance. Markdown mode returns
        concatenated text. Both modes stream under the hood.

        cache_system=True marks the system_message as cacheable (ephemeral
        prompt caching, ~5 min TTL). Only worth setting when the same
        system_message will be reused across multiple calls in quick
        succession (e.g. the comparison agent's 6+ per-clause calls).
        """

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        if mode == "JSON" and response_model is not None:
            return await self._generate_json(prompt, response_model, system_message, cache_system, max_tokens)
        return await self._generate_markdown(prompt, system_message, cache_system, max_tokens)

    @staticmethod
    def _build_system_field(system_message: Optional[str], cache_system: bool) -> Optional[Any]:
        """Return the body['system'] value, as a cacheable content block list when requested."""

        if not system_message:
            return None
        if cache_system:
            return [{"type": "text", "text": system_message, "cache_control": {"type": "ephemeral"}}]
        return system_message

    async def _generate_json(self, prompt: str, response_model: Type, system_message: Optional[str], cache_system: bool = False, max_tokens: int = 4096) -> Any:
        """Generate a JSON response by forcing tool-use against response_model's schema."""

        try:
            tool = {
                "name": response_model.__name__,
                "description": f"Submit a structured {response_model.__name__} response.",
                "input_schema": response_model.model_json_schema(),
            }

            body: Dict[str, Any] = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": response_model.__name__},
            }
            system_field = self._build_system_field(system_message, cache_system)
            if system_field is not None:
                body["system"] = system_field

            # Offload the blocking boto3 stream-drain to a worker thread so the
            # event loop stays free and concurrent agent calls actually run in parallel.
            tool_name, tool_input_json, usage = await asyncio.to_thread(self._collect_tool_use, body)

            self.logger.info(
                f"[bedrock-tokens] model={self.model_id} mode=json "
                f"input={usage.get('input_tokens', 0)} output={usage.get('output_tokens', 0)} "
                f"cache_write={usage.get('cache_creation_input_tokens', 0)} cache_read={usage.get('cache_read_input_tokens', 0)}"
            )

            if tool_name != response_model.__name__:
                raise EmptyResponseError("Bedrock returned no tool_use block for the requested response model. Try once more or debug the prompt.")

            tool_input = json.loads(tool_input_json) if tool_input_json else {}
            return response_model.model_validate(tool_input)

        except (EmptyResponseError, ResponseParsingError):
            raise
        except (json.JSONDecodeError, ValidationError) as e:
            self.logger.error(f"Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema: {str(e)}.")
            raise ResponseParsingError("Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema.") from e
        except Exception as e:
            self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
            raise LLMModelError("An error occurred while generating response from the LLM model.") from e

    async def _generate_markdown(self, prompt: str, system_message: Optional[str], cache_system: bool = False, max_tokens: int = 4096) -> str:
        """Generate a markdown/text response."""

        try:
            effective_system = system_message or "Extract the information and return valid Markdown format."
            body: Dict[str, Any] = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "system": self._build_system_field(effective_system, cache_system) or effective_system,
            }

            # Offload the blocking boto3 stream-drain to a worker thread (see _generate_json).
            response_text, usage = await asyncio.to_thread(self._collect_text, body)

            self.logger.info(
                f"[bedrock-tokens] model={self.model_id} mode=markdown "
                f"input={usage.get('input_tokens', 0)} output={usage.get('output_tokens', 0)} "
                f"cache_write={usage.get('cache_creation_input_tokens', 0)} cache_read={usage.get('cache_read_input_tokens', 0)}"
            )

            if not response_text:
                raise EmptyResponseError("Received empty response from LLM model, try once more or debug the prompt.")
            return response_text

        except EmptyResponseError:
            raise
        except Exception as e:
            self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
            raise LLMModelError("An error occurred while generating response from the LLM model.") from e
