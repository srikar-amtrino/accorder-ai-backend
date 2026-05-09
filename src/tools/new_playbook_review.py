import re
import unicodedata

from docx.document import Document

from src.config.logging import get_logger
from src.dependencies import get_service_container
from src.schemas.playbook_review import (
    PlayBookReviewFinalResponse,
    PlayBookReviewLLMResponse,
    PlayBookReviewResponse,
    RuleCheckRequest,
)
from src.services.session_manager import SessionData

logger = get_logger(__name__)


AGENT_NAME = "playbook_review_agent"


def normalize(text: str) -> str:
    if not text:
        return ""
    # Normalize unicode
    text = unicodedata.normalize("NFKC", text)
    # Replace all whitespace (including non-breaking) with single space
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


async def playbook_review_service(document: Document, request: RuleCheckRequest, session_data: SessionData) -> PlayBookReviewFinalResponse:
    """Function to review a document against playbook rules by parsing the document and storing it in session."""

    # Step 1: Parse the document using AI parser and store in session
    service_container = get_service_container()
    registry = service_container.ingestion_service.registry
    parser = registry.get_parser()

    parse_result = await parser.parse_document(document, session_data)

    if not parse_result.success or not parse_result.chunks:
        logger.error(f"Failed to parse document: {parse_result.error_message}")
        return PlayBookReviewFinalResponse(rules_review=[], missing_clauses=None)

    # Store chunks in session
    for chunk in parse_result.chunks:
        session_data.chunk_store[chunk.chunk_index] = chunk

    # Get document title from metadata
    document_title = parse_result.metadata.get("section_heading", "Unknown Document")
    logger.info(f"Parsed document '{document_title}' with {len(parse_result.chunks)} chunks")

    # Step 2: Review each rule against the parsed document
    rules_review = []

    for rule in request.rulesinformation:
        logger.info(f"Checking rule: {rule.title}")

        normalized_rule = normalize(rule.title)

        # Find the clause that matches the rule title from the parsed chunks
        matching_chunk = None
        for chunk in parse_result.chunks:
            section_heading = chunk.metadata.get("section_heading", "")
            if normalize(section_heading) == normalized_rule:
                matching_chunk = chunk
                break

        if not matching_chunk:
            logger.warning(f"No chunk found matching rule title: {rule.title}")
            # Return a response indicating the rule was not found
            llm_response = PlayBookReviewLLMResponse(para_identifiers=[], status="Not Found", reason=f"No clause found with title '{rule.title}'", suggestion="", suggested_fix="")
        else:
            # Step 3: Send the chunk content with rule description and instruction to LLM for validation
            llm_response = await validate_clause_against_rule(clause_content=matching_chunk.content, rule_title=rule.title, rule_description=rule.description, rule_instruction=rule.instruction)

        # Step 4: Format the response according to the schema
        rule_response = PlayBookReviewResponse(rule_title=rule.title, rule_instruction=rule.instruction, rule_description=rule.description, content=llm_response)
        rules_review.append(rule_response)

    return PlayBookReviewFinalResponse(rules_review=rules_review, missing_clauses=None)  # Could be implemented later if needed


async def validate_clause_against_rule(clause_content: str, rule_title: str, rule_description: str, rule_instruction: str) -> PlayBookReviewLLMResponse:
    """Validate a clause against a rule using LLM."""

    container = get_service_container()
    llm_model = container.azure_openai_model

    # Use the same prompt as the old playbook review
    from pathlib import Path

    prompt = Path(r"src\services\prompts\v1\ai_review_prompt_v2.mustache").read_text(encoding="utf-8")

    context = {"rule_title": rule_title, "rule_instruction": rule_instruction, "rule_description": rule_description, "paragraphs": f"PARA_ID: clause_content\nTEXT: {clause_content}"}

    try:
        response = await llm_model.generate(prompt=prompt, context=context, response_model=PlayBookReviewLLMResponse)
        return response
    except Exception as e:
        logger.error(f"LLM validation failed: {str(e)}")
        return PlayBookReviewLLMResponse(para_identifiers=["clause_content"], status="Error", reason=f"LLM validation failed: {str(e)}", suggestion="", suggested_fix="")
