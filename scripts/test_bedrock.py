"""Smoke test: confirm boto3 + .env + IAM role can call Claude on Bedrock."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3

from src.config.settings import get_settings


def main() -> None:
    settings = get_settings()

    if not settings.bedrock_model_id:
        raise SystemExit("BEDROCK_MODEL_ID is not set. Check your .env file.")

    print(f"Region:   {settings.aws_region}")
    print(f"Model id: {settings.bedrock_model_id}")
    print()
    print("Calling Bedrock with a 'say hello' prompt...")
    print()

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Summarize what an NDA is in two sentences."}],
    }

    response = client.invoke_model(
        modelId=settings.bedrock_model_id,
        body=json.dumps(body),
    )
    response_body = json.loads(response["body"].read())

    text = "".join(
        block.get("text", "")
        for block in response_body.get("content", [])
        if block.get("type") == "text"
    )

    print(f"Claude said: {text}")
    print()
    print("Bedrock connection works.")


if __name__ == "__main__":
    main()
