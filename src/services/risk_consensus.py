"""Self-consistency consensus for the Contract Analyzer.

A single Bedrock call at temperature 0 is not repeatable (no seed parameter), so
borderline clauses drift between otherwise-identical runs. This removes that
variance in code: run the analysis N times (default 3), ground each clause_title
to the clause index, majority-vote each clause (severity ties break to Critical),
then de-duplicate and order by document position. Model passed in (no DI
container) so it is testable directly.
"""

import asyncio
import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.schemas.contract_analyzer import ContractAnalyzerResponse, RiskComplianceInsight
from src.services.clause_index import build_clause_index, extract_clause_titles

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts" / "v3" / "contract_analyzer"
_SYSTEM_PROMPT = (_PROMPTS_DIR / "system.mustache").read_text(encoding="utf-8")
_USER_PROMPT = (_PROMPTS_DIR / "user.mustache").read_text(encoding="utf-8")

# Number of independent votes. 3 balances stability against token cost (~3x).
DEFAULT_VOTES = 3


def _norm(title: str) -> str:
    """Normalise a clause title for matching: lowercase, no trailing dots/space."""
    return re.sub(r"\s+", " ", (title or "").strip().strip(".").strip()).lower()


def _normalize_severity(severity: str) -> str:
    """Collapse the tier onto the two the rubric allows: Critical or High."""
    return "Critical" if (severity or "").strip().lower().startswith("crit") else "High"


def _ground_title(raw_title: str, index_norm: Dict[str, str]) -> Tuple[str, str]:
    """Map a model title onto a canonical clause-index title.

    Returns ``(key, display_title)``. A "Missing: ..." item is kept verbatim.
    A title that matches (exactly, by containment, or by close string match) an
    index entry adopts that entry's exact text. Anything else keeps its own text
    so a real clause the detector missed is not dropped.
    """
    key = _norm(raw_title)
    if not key:
        return "", raw_title

    if raw_title.strip().lower().startswith("missing:"):
        return key, raw_title.strip()

    if key in index_norm:
        return key, index_norm[key]

    # Containment: "non-compete" vs "non-compete covenant".
    for idx_key, idx_title in index_norm.items():
        if key == idx_key or key in idx_key or idx_key in key:
            return idx_key, idx_title

    # Fuzzy fall-back for minor wording differences.
    close = difflib.get_close_matches(key, list(index_norm.keys()), n=1, cutoff=0.82)
    if close:
        return close[0], index_norm[close[0]]

    return key, raw_title.strip()


def build_consensus(responses: List[ContractAnalyzerResponse], index_titles: List[str]) -> ContractAnalyzerResponse:
    """Merge N analyzer responses into one stable consensus response."""
    if not responses:
        raise ValueError("build_consensus requires at least one response")

    n = len(responses)
    threshold = (n // 2) + 1  # simple majority (3 -> 2, 2 -> 2, 1 -> 1)

    index_norm = {_norm(t): t for t in index_titles}
    index_order = {_norm(t): i for i, t in enumerate(index_titles)}

    # key -> aggregated votes
    votes: Dict[str, Dict[str, Any]] = {}
    first_seen: Dict[str, int] = {}
    order_counter = 0

    for resp in responses:
        seen_in_resp = set()
        for item in resp.risk_and_compliance_insights:
            key, title = _ground_title(item.clause_title, index_norm)
            if not key or key in seen_in_resp:
                continue  # drop empties and within-run duplicates
            seen_in_resp.add(key)
            bucket = votes.setdefault(key, {"title": title, "severities": [], "issues": []})
            bucket["severities"].append(_normalize_severity(item.severity))
            if item.issue and item.issue.strip():
                bucket["issues"].append(item.issue.strip())
            if key not in first_seen:
                first_seen[key] = order_counter
                order_counter += 1

    consensus_keys = [k for k, b in votes.items() if len(b["severities"]) >= threshold]

    def sort_key(k: str) -> Tuple[int, int]:
        # In-index clauses first, in document order; off-index items after, in first-seen order.
        if k in index_order:
            return (0, index_order[k])
        return (1, first_seen.get(k, 0))

    consensus_keys.sort(key=sort_key)

    risks: List[RiskComplianceInsight] = []
    for key in consensus_keys:
        bucket = votes[key]
        severity = _majority_severity(bucket["severities"])
        issue = _pick_issue(bucket["issues"])
        risks.append(RiskComplianceInsight(severity=severity, clause_title=bucket["title"], issue=issue))

    canonical = _pick_canonical(responses, set(consensus_keys), index_norm)

    return ContractAnalyzerResponse(
        summary=canonical.summary,
        key_information=canonical.key_information,
        timeline_and_key_milestones=canonical.timeline_and_key_milestones,
        risk_and_compliance_insights=risks,
    )


def _majority_severity(severities: List[str]) -> str:
    """Mode of the tiers; a Critical/High tie breaks to the safer Critical."""
    crit = severities.count("Critical")
    high = severities.count("High")
    return "Critical" if crit >= high else "High"


def _pick_issue(issues: List[str]) -> str:
    """Pick a stable, terse issue string: the shortest non-empty, ties by text."""
    if not issues:
        return ""
    return sorted(issues, key=lambda s: (len(s), s))[0]


def _pick_canonical(
    responses: List[ContractAnalyzerResponse], consensus_keys: set, index_norm: Dict[str, str]
) -> ContractAnalyzerResponse:
    """Choose the run whose risk set best matches consensus for the prose sections."""
    best = responses[0]
    best_score = -1.0
    for resp in responses:
        keys = set()
        for item in resp.risk_and_compliance_insights:
            k, _ = _ground_title(item.clause_title, index_norm)
            if k:
                keys.add(k)
        union = keys | consensus_keys
        score = (len(keys & consensus_keys) / len(union)) if union else 1.0
        if score > best_score:
            best_score = score
            best = resp
    return best


async def analyze_contract_consensus(model: Any, content: str, session_id: str, num_votes: int = DEFAULT_VOTES) -> ContractAnalyzerResponse:
    """Run the analyzer ``num_votes`` times in parallel and merge to consensus."""
    clause_index = build_clause_index(content)
    context = {"contract_text": content, "clause_index": clause_index}

    async def _one() -> ContractAnalyzerResponse:
        return await model.generate(
            prompt=_USER_PROMPT,
            context=context,
            response_model=ContractAnalyzerResponse,
            session_id=session_id,
            system_message=_SYSTEM_PROMPT,
            temperature=0.0,
        )

    results = await asyncio.gather(*[_one() for _ in range(num_votes)], return_exceptions=True)
    responses = [r for r in results if isinstance(r, ContractAnalyzerResponse)]
    if not responses:
        # Surface the first real error if every vote failed.
        for r in results:
            if isinstance(r, Exception):
                raise r
        raise RuntimeError("Contract analysis produced no responses")

    return build_consensus(responses, extract_clause_titles(content))
