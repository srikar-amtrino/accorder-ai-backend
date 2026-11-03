from typing import Dict, List, Sequence

from src.schemas.clause_mapping import ClassificationStatus, ClauseClassification, ClauseMatch

# Cosine-similarity cut-offs for bge-m3 against the shared collection, where a
# whole clause is matched to sentence-level chunks: the right type lands around
# 0.79–0.90 and unrelated types stay below ~0.73. Re-tune if the embedding
# model or the chunking changes.
AUTO_ACCEPT_SCORE = 0.80
CANDIDATE_SCORE = 0.75

# Distinct clause types considered per clause, both for compound-clause
# detection and as the candidate list for LLM confirmation.
MAX_CANDIDATES = 3


def _best_per_type(hits: Sequence[ClauseMatch]) -> List[ClauseMatch]:
    """Keep each canonical type's highest-scoring hit, best first.

    The vector DB holds several example records per clause type, so raw top-k
    hits often repeat one type; ranking distinct types keeps the top three
    meaningful.
    """

    best: Dict[str, ClauseMatch] = {}
    for hit in hits:
        current = best.get(hit.canonical_type)
        if current is None or hit.score > current.score:
            best[hit.canonical_type] = hit
    return sorted(best.values(), key=lambda hit: hit.score, reverse=True)


def classify_clause(hits: Sequence[ClauseMatch]) -> ClauseClassification:
    """Decide which clause type(s) a clause maps to from its vector DB hits.

    - Top score >= AUTO_ACCEPT_SCORE: accepted. Every other type in the top
      three that also clears it is accepted too, so a compound clause
      ("Indemnification and Limitation of Liability") carries all its policies.
    - Top score >= CANDIDATE_SCORE: up to three candidate types, each at or
      above CANDIDATE_SCORE, for the LLM to pick from or reject.
    - Lower, or no hits: unclassified — flag for manual triage.

    Query the vector DB with a top_k well above three (filtered by agreement
    type when known): hits are deduplicated per clause type before ranking.
    """

    ranked = _best_per_type(hits)[:MAX_CANDIDATES]
    if not ranked or ranked[0].score < CANDIDATE_SCORE:
        return ClauseClassification(status=ClassificationStatus.UNCLASSIFIED)
    if ranked[0].score >= AUTO_ACCEPT_SCORE:
        accepted = [hit for hit in ranked if hit.score >= AUTO_ACCEPT_SCORE]
        return ClauseClassification(status=ClassificationStatus.AUTO_ACCEPTED, matches=accepted)
    candidates = [hit for hit in ranked if hit.score >= CANDIDATE_SCORE]
    return ClauseClassification(status=ClassificationStatus.NEEDS_CONFIRMATION, matches=candidates)
