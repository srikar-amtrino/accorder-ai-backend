from pathlib import Path

from src.dependencies import get_service_container
from src.schemas.describe_and_draft import DraftRequest, DraftResponse

AGENT_NAME = "describe_and_draft"


async def draft_document(request: DraftRequest, session_id: str) -> DraftResponse:
    """Draft the document for the user query."""

    container = get_service_container()
    llm_model = container.azure_openai_model

    session_data = container.session_manager.get_session(session_id=session_id)
    if not session_data:
        return {}

    agent_results = session_data.tool_results.setdefault(AGENT_NAME, {})

    prompt = Path(r"src\services\prompts\v1\describe_and_draft_prompt.mustache").read_text(encoding="utf-8")

    # Build previous versions summary to inject into the prompt
    previous_versions = {}
    for clause_title, clause_data in agent_results.items():
        previous_versions[clause_title] = {"summary": clause_data["summary"], "versions": clause_data["versions"]}

    generated_content: DraftResponse = await llm_model.generate(
        prompt=prompt,
        context={"user_query": request.user_query, "previous_versions": previous_versions if previous_versions else ""},
        response_model=DraftResponse,
    )

    for clause in generated_content.data:
        title = clause.clause_title

        # Initialize clause entry if it does not exist
        if title not in agent_results:
            agent_results[title] = {"summary": clause.summary, "versions": []}  # set only once

        clause_entry = agent_results[title]

        # Determine next version number
        next_version = len(clause_entry["versions"]) + 1

        clause_entry["versions"].append({"version": next_version, "content": clause.content})

    return generated_content
