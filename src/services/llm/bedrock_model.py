import json
from typing import Any, Dict, Optional, Type, Union

import boto3
import pystache
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

    AWS credentials are not handled here. They come from the standard
    credential chain: IAM role on EC2, or env vars / ~/.aws/credentials
    locally.
    """

    def __init__(self) -> None:
        """Initialize the Bedrock runtime client."""

        super().__init__()
        self.settings = get_settings()

        # Settings fields are added in Phase 2; read defensively so this
        # module is importable on its own.
        self.model_id: Union[str, None] = getattr(self.settings, "bedrock_model_id", None)
        if not self.model_id:
            raise LLMModelError("Bedrock model id is not configured. Set 'BEDROCK_MODEL_ID' in the environment.")

        self.region: Union[str, None] = getattr(self.settings, "aws_region", None)
        if not self.region:
            raise LLMModelError("AWS region is not configured. Set 'AWS_REGION' in the environment.")

        self.client = boto3.client("bedrock-runtime", region_name=self.region)

    def render_prompt_template(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Mustache prompt template render function."""

        return pystache.render(template=prompt, context=context)

    async def stream(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Stream response generation function."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        try:
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 16384,
                "messages": [{"role": "user", "content": prompt}],
                "system": "Extract the information and return valid JSON.",
            }

            response = self.client.invoke_model_with_response_stream(
                modelId=self.model_id,
                body=json.dumps(body),
            )

            for event in response["body"]:
                chunk_bytes = event.get("chunk", {}).get("bytes")
                if not chunk_bytes:
                    continue
                chunk = json.loads(chunk_bytes)
                if chunk.get("type") != "content_block_delta":
                    continue
                delta = chunk.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")

        except Exception as e:
            self.logger.error(f"An error occurred while streaming response from the LLM model: {str(e)}")
            raise LLMModelError("An error occurred while streaming response from the LLM model.") from e

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Union[Type, None], mode: str = "JSON", system_message: Optional[str] = None) -> Any:
        """Main function to generate response."""

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        if mode == "JSON" and response_model is not None:
            try:
                tool = {
                    "name": response_model.__name__,
                    "description": f"Submit a structured {response_model.__name__} response.",
                    "input_schema": response_model.model_json_schema(),
                }

                body: Dict[str, Any] = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 16384,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [tool],
                    "tool_choice": {"type": "tool", "name": response_model.__name__},
                }
                if system_message:
                    body["system"] = system_message

                response = self.client.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps(body),
                )
                response_body = json.loads(response["body"].read())

                tool_input: Union[Dict[str, Any], None] = None
                for block in response_body.get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == response_model.__name__:
                        tool_input = block.get("input")
                        break

                if tool_input is None:
                    raise EmptyResponseError("Bedrock returned no tool_use block for the requested response model. Try once more or debug the prompt.")

                validated_response = response_model.model_validate(tool_input)
                return validated_response

            except (EmptyResponseError, ResponseParsingError):
                raise
            except (json.JSONDecodeError, ValidationError) as e:
                self.logger.error(f"Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema: {str(e)}.")
                raise ResponseParsingError("Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema.") from e
            except Exception as e:
                self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
                raise LLMModelError("An error occurred while generating response from the LLM model.") from e

        else:
            try:
                body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 16384,
                    "messages": [{"role": "user", "content": prompt}],
                    "system": system_message or "Extract the information and return valid Markdown format.",
                }

                response = self.client.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps(body),
                )
                response_body = json.loads(response["body"].read())

                text_chunks = [block.get("text", "") for block in response_body.get("content", []) if block.get("type") == "text"]
                response_text = "".join(text_chunks)

                if not response_text:
                    raise EmptyResponseError("Received empty response from LLM model, try once more or debug the prompt.")
                return response_text

            except EmptyResponseError:
                raise
            except Exception as e:
                self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
                raise LLMModelError("An error occurred while generating response from the LLM model.") from e
