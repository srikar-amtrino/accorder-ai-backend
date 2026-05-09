import json
from typing import Any, Dict, Type

import pystache
from google import genai
from google.genai import types
from pydantic import ValidationError

from src.config.logging import Logger
from src.config.settings import get_settings
from src.services.llm.base_model import BaseLLMModel


class GeminiModel(BaseLLMModel, Logger):
    """Gemini LLM model for generating responsess."""

    def __init__(self) -> None:
        """Initialize the gemini model."""

        super().__init__()
        self.settings = get_settings()
        self.api_key = self.settings.gemini_api_key

        if self.api_key is None:
            raise ValueError("Gemini Key was not configured in the environment variables.")
        self.client = genai.Client(
            api_key=self.settings.gemini_api_key,
        )

    def render_prompt_template(self, prompt: str, context: Dict[str, Any]) -> Any:
        """Mustache prompt template render function."""

        return pystache.render(template=prompt, context=context)

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Type) -> Any:
        """Main function to generate responses"""

        # Format the prompt with the context.
        prompt = self.render_prompt_template(prompt=prompt, context=context)

        response = None
        try:
            response = await self.client.aio.models.generate_content(
                model=self.settings.gemini_text_generation_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_model,
                ),
            )

            # Parse the JSON response text and validate against response_model
            response_data = json.loads(response.text)
            validated_response = response_model.model_validate(response_data)
            return validated_response
        except (json.JSONDecodeError, ValidationError) as e:
            raw_text = response.text if response else "N/A"
            self.logger.error(f"Response parsing failed: {str(e)}. Raw response: {raw_text}")
            raise ValueError("Cannot perform the query rewriting.") from e
        except Exception as e:
            self.logger.error(f"LLM generation failed: {str(e)}")
            raise ValueError("Cannot perform the query rewriting.") from e
