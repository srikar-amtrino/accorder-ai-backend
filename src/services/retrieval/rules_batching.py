import asyncio
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from src.config.logging import get_logger
from src.dependencies import get_service_container
from src.schemas.playbook_review import (
    MissingClausesLLMResponse,
    ParaSimilarity,
    RuleCheckRequest,
    RuleResult,
    TextInfo,
)

logger = get_logger(__name__)


async def get_missing_clauses(data: str) -> List[str]:
    """Get the missing clauses for the given contract text."""

    service_container = get_service_container()
    llm_model = service_container.azure_openai_model

    prompt = Path(r"src\services\prompts\v1\missing_clauses.mustache").read_text(encoding="utf-8")
    context = {"data": data}
    response = await llm_model.generate(
        prompt=prompt,
        context=context,
        response_model=MissingClausesLLMResponse,
    )

    # Simple parsing of bullet points from the response
    missing_clauses = [line.strip("- ").strip() for line in response.splitlines() if line.startswith("-")]

    logger.info(f"Identified {len(missing_clauses)} missing clauses.")

    return missing_clauses


async def get_matching_pairs_faiss(request: RuleCheckRequest) -> List[RuleResult]:
    """Get the matching pairs for the given rules with FAISS."""

    service_container = get_service_container()
    faiss_db = service_container.faiss_store
    embedding_model = service_container.embedding_service

    # Index all paragraph embeddings into FAISS
    for item in request.textinformation:
        embedd_vector = await embedding_model.generate_embeddings(item.text)
        logger.info(f"Indexing paragraph {item.paraindetifier} into FAISS.")
        await faiss_db.index_embedding(embedd_vector)

    results: List[RuleResult] = []

    for rule in request.rulesinformation:
        rule_text = f"title: {rule.title}. " f"description: {rule.description}. "  #  f"tags: {', '.join(rule.tags)}
        logger.info(f"Generating embedding for rule '{rule.title}'.")
        rule_embedds = await embedding_model.generate_embeddings(rule_text)
        logger.info(f"Searching for similar paragraphs in FAISS for rule '{rule.title}'.")
        faiss_result: Dict[str, Any] = await faiss_db.search_index(rule_embedds, top_k=3)

        indices = faiss_result.get("indices", [])
        scores = faiss_result.get("scores", [])

        # Filter out invalid FAISS indices (-1 means no result found)
        matched_pairs: List[ParaSimilarity] = [(idx, score) for idx, score in zip(indices, scores) if idx != -1 and idx < len(request.textinformation)]

        if not matched_pairs:
            logger.info(f"No relevant paragraphs found in FAISS for rule '{rule.title}'.")
            results.append(
                RuleResult(
                    title=rule.title,
                    instruction=rule.instruction,
                    description="No relevant contract paragraphs found.",
                    paragraphidentifier="",
                    paragraphcontext="",
                    similarity_scores=[],
                )
            )
            continue

        logger.info(f"Found {len(matched_pairs)} similar paragraphs in FAISS for rule '{rule.title}'.")

        matched_paras = [request.textinformation[idx] for idx, _ in matched_pairs]
        similarity_scores = [float(score) for _, score in matched_pairs]

        para_ids = ",".join(p.paraindetifier for p in matched_paras)
        para_context = "\n\n".join(f"[{p.paraindetifier}] {p.text.strip()}" for p in matched_paras)

        results.append(
            RuleResult(
                title=rule.title,
                instruction=rule.instruction,
                description=rule.description,
                paragraphidentifier=para_ids,
                paragraphcontext=para_context,
                similarity_scores=similarity_scores,
            )
        )

    return results


def find_similarity(rule_embedd: np.ndarray, para_embedds: np.ndarray, para_items: List[TextInfo], top_k: int = 3, threshold: float = 0.30) -> List[ParaSimilarity]:
    """get the similar paragraphs for the given rules."""

    logger.info("Normalizing embeddings for similarity computation.")
    # Normalize safely
    rules_norm = rule_embedd / (np.linalg.norm(rule_embedd) + 1e-10)
    para_norms = para_embedds / (np.linalg.norm(para_embedds, axis=1, keepdims=True) + 1e-10)

    logger.info("Computing cosine similarity between rule and paragraph embeddings.")
    # compute cosine similarity
    scores = para_norms @ rules_norm

    logger.info("Sorting similarity scores and filtering based on threshold.")
    # Sort indices descending
    top_indices = np.argsort(scores)[::-1][:top_k]

    results: List[ParaSimilarity] = []
    for idx in top_indices:
        if scores[idx] >= threshold:
            results.append({"paragraph": para_items[idx], "similarity": float(scores[idx])})

    logger.info(f"Found {len(results)} paragraphs with similarity above the threshold of {threshold}.")

    return results


async def get_matching_paras(request: RuleCheckRequest) -> List[RuleResult]:
    """Get the matching paras for the given rules."""

    service_container = get_service_container()
    embedding_model = service_container.embedding_service

    rule_texts = [f"title: {rule.title}. " f"description: {rule.description}." f"tags: {', '.join(rule.tags)}" for rule in request.rulesinformation]
    # rule_texts = [f"title: {rule.title}. " f"description: {rule.description}." for rule in request.rulesinformation]

    logger.info("Generating embeddings for rules and paragraphs.")
    rule_embeddings = np.array(await asyncio.gather(*[embedding_model.generate_embeddings(text) for text in rule_texts]))
    para_embeddings = np.array(await asyncio.gather(*[embedding_model.generate_embeddings(item.text) for item in request.textinformation]))

    results: List[RuleResult] = []

    for rule, rule_emb in zip(request.rulesinformation, rule_embeddings):

        logger.info(f"Finding similar paragraphs for rule '{rule.title}'.")
        matched: List[ParaSimilarity] = find_similarity(
            rule_embedd=rule_emb,
            para_embedds=para_embeddings,
            para_items=request.textinformation,
            top_k=3,
            threshold=0.30,
        )

        if not matched:
            logger.info(f"No relevant paragraphs found for rule '{rule.title}'.")
            results.append(
                RuleResult(
                    title=rule.title,
                    instruction=rule.instruction,
                    description="No relevant contract paragraphs found.",
                    paragraphidentifier="",
                    paragraphcontext="",
                    similarity_scores=[],
                )
            )
            continue

        para_ids = ",".join(m["paragraph"].paraindetifier for m in matched)
        para_context = "\n\n".join(f"[{m['paragraph'].paraindetifier}] {m['paragraph'].text.strip()}" for m in matched)
        similarity_scores = [m["similarity"] for m in matched]

        results.append(
            RuleResult(
                title=rule.title,
                instruction=rule.instruction,
                description=rule.description,
                paragraphidentifier=para_ids,
                paragraphcontext=para_context,
                similarity_scores=similarity_scores,
            )
        )

    return results
