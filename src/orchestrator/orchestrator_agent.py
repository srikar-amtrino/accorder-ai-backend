import asyncio
import sys
from pathlib import Path

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from agent_framework import ChatAgent
from agent_framework.openai import OpenAIResponsesClient

from src.config.settings import get_settings
from src.tools.key_information import get_key_information
from src.tools.summarizer import get_summary

settings = get_settings()

prompt = Path(r"src\services\prompts\v1\orchestrator_prompt.mustache").read_text()


async def get_azure_agent() -> ChatAgent:

    agent = ChatAgent(
        chat_client=OpenAIResponsesClient(
            model_id=settings.azure_openai_responses_deployment_name,
            api_key=settings.azure_openai_api_key,
            base_url=settings.base_url,
        ),
        instructions=prompt,
        tools=[get_summary, get_key_information],
    )

    return agent


if __name__ == "__main__":
    asyncio.run(get_azure_agent())
