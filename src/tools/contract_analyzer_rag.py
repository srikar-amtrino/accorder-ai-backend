"""RAG variant of the Contract Analyzer: chunk -> embed -> retrieve -> analyze.

Analyzes only the passages retrieved for a set of risk queries, instead of the
whole contract. Takes the model and an embed function as args (no DI container)
so it is testable directly. Diagnostics report document coverage.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from src.schemas.contract_analyzer import ContractAnalyzerResponse

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3" / "contract_analyzer"
_SYSTEM_PROMPT = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_USER_PROMPT = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# One query per risk dimension; a single "find risks" query retrieves poorly.
RISK_QUERIES: List[str] = [
    "limitation of liability cap on damages",
    "indemnification hold harmless obligations",
    "intellectual property ownership assignment of work product",
    "non-compete non-solicitation restrictive covenant",
    "termination rights and forfeiture of fees or advances",
    "confidentiality and non-disclosure obligations",
    "assignment of the agreement and consent",
    "unilateral right to change scope price or terms",
    "payment terms timing and late payment",
    "warranties and representations",
    "governing law dispute resolution and arbitration",
    "power of attorney granted to a party",
]

DEFAULT_TOP_K = 4  # passages retrieved per query


def chunk_text(content: str, target_chars: int = 700) -> List[str]:
    """Group paragraphs into passages of ~target_chars, keeping clauses intact."""
    paras = [p.strip() for p in content.splitlines() if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paras:
        if buf and len(buf) + len(para) + 1 > target_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _cosine_top_k(query_vec: List[float], chunk_vecs: List[List[float]], k: int) -> List[int]:
    import numpy as np

    q = np.asarray(query_vec, dtype=np.float32)
    m = np.asarray(chunk_vecs, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-10)
    m = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-10)
    scores = m @ q
    return [int(i) for i in np.argsort(scores)[::-1][:k]]


async def analyze_contract_rag(
    model: Any,
    embed_fn: Callable[[List[str]], List[List[float]]],
    content: str,
    session_id: str,
    top_k: int = DEFAULT_TOP_K,
) -> Tuple[ContractAnalyzerResponse, Dict[str, Any]]:
    """Retrieve risk-relevant passages, then analyze only those."""
    chunks = chunk_text(content)
    if not chunks:
        raise ValueError("No text to analyze")

    chunk_vecs = embed_fn(chunks)
    query_vecs = embed_fn(RISK_QUERIES)

    # Union of top-k passages across all risk queries, kept in document order.
    retrieved: set = set()
    for qv in query_vecs:
        retrieved.update(_cosine_top_k(qv, chunk_vecs, top_k))
    retrieved_order = sorted(retrieved)
    retrieved_context = "\n\n".join(chunks[i] for i in retrieved_order)

    response: ContractAnalyzerResponse = await model.generate(
        prompt=_USER_PROMPT,
        context={"contract_text": retrieved_context},
        response_model=ContractAnalyzerResponse,
        session_id=session_id,
        system_message=_SYSTEM_PROMPT,
        temperature=0.0,
    )

    diagnostics = {
        "total_chunks": len(chunks),
        "retrieved_chunks": len(retrieved_order),
        "coverage_pct": round(100 * len(retrieved_order) / len(chunks)),
        "total_chars": len(content),
    }
    return response, diagnostics
