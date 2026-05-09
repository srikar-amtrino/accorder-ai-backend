import json
from typing import Any, Dict, Type, Union

import pystache
from openai import OpenAI
from pydantic import ValidationError

from src.config.logging import Logger
from src.config.settings import get_settings
from src.exceptions.llm_exceptions import (
    APIKeyNotConfigured,
    BaseURLNotConfigured,
    DeploymentNotConfigured,
    EmptyResponseError,
    LLMModelError,
    ResponseParsingError,
)
from src.services.llm.base_model import BaseLLMModel


class AzureOpenAIModel(BaseLLMModel, Logger):
    """Azure Open AI class for generating responses."""

    def __init__(self) -> None:
        """Initialize the agent."""

        super().__init__()
        self.settings = get_settings()

        if not self.settings.azure_openai_api_key:
            raise APIKeyNotConfigured("Azure OpenAI API key is not configured. Add it to the environment variables or configuration file as 'AZURE_OPENAI_API_KEY'.")

        if not self.settings.azure_openai_responses_deployment_name:
            raise DeploymentNotConfigured("Azure OpenAI deployment name is not configured. Add it to the environment variables or configuration file as 'AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME'.")

        if not self.settings.base_url:
            raise BaseURLNotConfigured("Azure Base URL is not configured. Add it to the environment variables or configuration file as 'BASE_URL'.")

        self.client = OpenAI(
            base_url=self.settings.base_url,
            api_key=self.settings.azure_openai_api_key,
            # azure_endpoint=self.settings.base_url,
            # api_version=self.settings.azure_api_version,
        )

        self.deployment_name = self.settings.azure_openai_responses_deployment_name

    def render_prompt_template(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Mustache prompt template render function."""

        return pystache.render(template=prompt, context=context)

    async def stream(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Stream response generation function."""

        if self.deployment_name is None:
            raise ValueError("Deployment name is not configured.")

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {"role": "system", "content": "Extract the information and return valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                stream=True,
            )

            for event in response:
                yield event.choices[0].delta.content

        except Exception as e:
            self.logger.error(f"An error occurred while streaming response from the LLM model: {str(e)}")
            raise LLMModelError("An error occurred while streaming response from the LLM model.") from e

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Union[Type, None], mode: str = "JSON") -> Any:
        """Main function to generate response."""

        if self.deployment_name is None:
            raise ValueError("Deployment name is not configured.")

        prompt = self.render_prompt_template(prompt=prompt, context=context)
        self.logger.info(f"Updated prompt for passing to the LLM: {prompt}")

        if mode == "JSON" and response_model is not None:
            try:
                json_schema = response_model.model_json_schema()

                response = self.client.chat.completions.create(
                    model=self.deployment_name,
                    messages=[
                        {"role": "system", "content": f"Extract the information and return valid {mode} format."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    # seed=42,
                    max_tokens=15000,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": response_model.__name__,
                            "schema": json_schema,
                            "strict": False,
                        },
                    },
                )

                # Extract and parse the JSON response
                response_text = response.choices[0].message.content
                if response_text is None:
                    raise EmptyResponseError("Received empty response from LLM model, try once more or debug the prompt.")
                response_data = json.loads(response_text)

                # Validate and convert to Pydantic model
                validated_response = response_model.model_validate(response_data)
                return validated_response

            except (json.JSONDecodeError, ValidationError) as e:
                self.logger.error(f"Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema: {str(e)}.")
                raise ResponseParsingError("Failed to parse the LLM response. The response format might be incorrect or not matching the expected schema.") from e
            except Exception as e:
                self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
                raise LLMModelError("An error occurred while generating response from the LLM model.") from e

        else:
            try:
                response = self.client.chat.completions.create(
                    model=self.deployment_name,
                    messages=[
                        {"role": "system", "content": "Extract the information and return valid Markdown format."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                response_text = response.choices[0].message.content
                if response_text is None:
                    raise EmptyResponseError("Received empty response from LLM model, try once more or debug the prompt.")
                return response_text

            except Exception as e:
                self.logger.error(f"An error occurred while generating response from the LLM model: {str(e)}")
                raise LLMModelError("An error occurred while generating response from the LLM model.") from e
