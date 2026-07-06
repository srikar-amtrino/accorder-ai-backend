"""Majority-vote consensus for the Contract Analyzer risk output.

A single model call is not repeatable, so borderline clauses can drift between
otherwise-identical runs. This module removes that variance in code: it grounds
each clause_title to the clause index, keeps a clause only when a majority of
the independent analyses report it, takes the majority severity (ties break to
the more severe tier), then de-duplicates and orders by document position. The
model instance is passed in so the module stays container-free and testable.
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


# Lower rank = more severe; used for majority tie-breaks (tie -> the safer, more
# severe tier). "Low" is not a reportable tier — items graded Low are dropped.
_SEVERITY_RANK = {"Critical": 0, "High": 1, "Medium": 2}


def _normalize_severity(severity: str) -> str:
    """Map the model's tier text onto the rubric tiers: Critical / High / Medium / Low."""
    s = (severity or "").strip().lower()
    if s.startswith("crit"):
        return "Critical"
    if s.startswith("med"):
        return "Medium"
    if s.startswith("low"):
        return "Low"
    return "High"


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


def build_risk_consensus(risk_lists: List[List[RiskComplianceInsight]], index_titles: List[str]) -> List[RiskComplianceInsight]:
    """Majority-vote N risk lists into one stable, grounded, ordered list."""
    risks, _ = _vote_risks(risk_lists, index_titles)
    return risks


def _vote_risks(
    risk_lists: List[List[RiskComplianceInsight]], index_titles: List[str]
) -> Tuple[List[RiskComplianceInsight], set]:
    if not risk_lists:
        raise ValueError("risk consensus requires at least one risk list")

    n = len(risk_lists)
    threshold = (n // 2) + 1  # simple majority (3 -> 2, 2 -> 2, 1 -> 1)

    index_norm = {_norm(t): t for t in index_titles}
    index_order = {_norm(t): i for i, t in enumerate(index_titles)}

    # key -> aggregated votes
    votes: Dict[str, Dict[str, Any]] = {}
    first_seen: Dict[str, int] = {}
    order_counter = 0

    for risk_list in risk_lists:
        seen_in_resp = set()
        for item in risk_list:
            severity = _normalize_severity(item.severity)
            if severity == "Low":
                continue  # Low is below the reporting threshold
            key, title = _ground_title(item.clause_title, index_norm)
            if not key or key in seen_in_resp:
                continue  # drop empties and within-run duplicates
            seen_in_resp.add(key)
            bucket = votes.setdefault(key, {"title": title, "severities": [], "issues": [], "para_identifiers": []})
            bucket["severities"].append(severity)
            if item.issue and item.issue.strip():
                bucket["issues"].append(item.issue.strip())
            pid = (getattr(item, "para_identifier", "") or "").strip()
            if pid:
                bucket["para_identifiers"].append(pid)
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
        para_identifier = _pick_para_identifier(bucket["para_identifiers"])
        risks.append(RiskComplianceInsight(severity=severity, clause_title=bucket["title"], para_identifier=para_identifier, issue=issue))

    return risks, set(consensus_keys)


def build_consensus(responses: List[ContractAnalyzerResponse], index_titles: List[str]) -> ContractAnalyzerResponse:
    """Merge N analyzer responses into one stable consensus response."""
    if not responses:
        raise ValueError("build_consensus requires at least one response")

    index_norm = {_norm(t): t for t in index_titles}
    risks, consensus_keys = _vote_risks([r.risk_and_compliance_insights for r in responses], index_titles)

    canonical = _pick_canonical(responses, consensus_keys, index_norm)

    return ContractAnalyzerResponse(
        summary=canonical.summary,
        key_information=canonical.key_information,
        timeline_and_key_milestones=canonical.timeline_and_key_milestones,
        risk_and_compliance_insights=risks,
    )


def _majority_severity(severities: List[str]) -> str:
    """Mode of the tiers; a tie breaks to the more severe (safer) tier."""
    counts: Dict[str, int] = {}
    for s in severities:
        counts[s] = counts.get(s, 0) + 1
    return max(counts, key=lambda s: (counts[s], -_SEVERITY_RANK[s]))


def _pick_issue(issues: List[str]) -> str:
    """Pick a stable, terse issue string: the shortest non-empty, ties by text."""
    if not issues:
        return ""
    return sorted(issues, key=lambda s: (len(s), s))[0]


def _pick_para_identifier(identifiers: List[str]) -> str:
    """Most common paragraph identifier for the clause; ties break by text for stability."""
    if not identifiers:
        return ""
    counts: Dict[str, int] = {}
    for pid in identifiers:
        counts[pid] = counts.get(pid, 0) + 1
    return sorted(counts, key=lambda p: (-counts[p], p))[0]


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
