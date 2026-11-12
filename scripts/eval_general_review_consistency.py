"""Measure how consistent the general review is from one cold run to the next, against the live model.

The unit tests script the model, so they cannot show whether the real one
flags the same paragraphs twice. This script runs the review several times on
the same request with every cache emptied between runs, and reports:

    - how often each paragraph was flagged, and at which risk levels;
    - flagged-paragraph agreement (mean pairwise Jaccard, and the share of
      flagged paragraphs that every run flagged);
    - risk-level agreement on the paragraphs every run flagged;
    - how alike the suggested fixes are from run to run (identical share and
      mean text similarity);
    - where the review exposes a trace, whether each flagged paragraph was
      redrafted to resolve the same points in every run.

It also writes every run's suggestions, and each paragraph's fixes side by
side, to the output directory, so the drafting can be read and compared.

    poetry run python scripts/eval_general_review_consistency.py --runs 5
    poetry run python scripts/eval_general_review_consistency.py --fixture my_contract.json --runs 3 --clause-search

It makes real Bedrock calls with the credentials in .env. The clause vector
DB is left out unless --clause-search is given, so clause types come from the
offline heading classifier and the run does not depend on Qdrant being up.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "master_services_agreement.json"


def _clear_caches(review_module: Any) -> None:
    """Empty every in-process cache so the next run is cold."""

    for name in ("_response_cache", "clause_cache"):
        cache = getattr(review_module, name, None)
        if cache is not None:
            cache.clear()


def _jaccard(left: set, right: set) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _points_by_paragraph(trace: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    points: Dict[str, List[str]] = {}
    for entry in trace:
        points.update(entry.get("points", {}))
    return points


def summarise(runs: List[List[Dict[str, Any]]], order: List[str], traces: Optional[List[List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Agreement figures for a list of runs, each a list of suggestion dicts."""

    flagged = [{item["para_identifier"] for item in run} for run in runs]
    levels = [{item["para_identifier"]: item["risk_level"] for item in run} for run in runs]
    ever = set().union(*flagged) if flagged else set()
    always = set.intersection(*flagged) if flagged else set()
    pairs = list(combinations(range(len(runs)), 2))
    same_level = [para for para in always if len({run[para] for run in levels}) == 1]
    per_paragraph: Dict[str, Dict[str, Any]] = {
        para: {"flagged_in": sum(para in run for run in flagged), "risk_levels": dict(Counter(run[para] for run in levels if para in run))} for para in sorted(ever, key=order.index)
    }
    fixes = [{item["para_identifier"]: " ".join(item["suggested_fix"].split()) for item in run} for run in runs]
    identical = [para for para in always if len({run[para] for run in fixes}) == 1]
    similarity = [SequenceMatcher(None, fixes[a][para], fixes[b][para], autojunk=False).ratio() for para in always for a, b in pairs]
    extra: Dict[str, Any] = {}
    if traces and any(traces):
        points = [_points_by_paragraph(trace) for trace in traces]
        same_points = [para for para in always if len({tuple(sorted(run.get(para, []))) for run in points}) == 1]
        extra["same_points_on_always_flagged"] = round(len(same_points) / len(always), 3) if always else 1.0
        for para, row in per_paragraph.items():
            row["points"] = [sorted(run.get(para, [])) for run in points]
    return {
        **extra,
        "identical_fix_share_on_always_flagged": round(len(identical) / len(always), 3) if always else 1.0,
        "mean_fix_similarity_on_always_flagged": round(sum(similarity) / len(similarity), 3) if similarity else 1.0,
        "runs": len(runs),
        "suggestions_per_run": [len(run) for run in runs],
        "paragraphs_flagged_in_any_run": len(ever),
        "paragraphs_flagged_in_every_run": len(always),
        "flagged_set_stability": round(len(always) / len(ever), 3) if ever else 1.0,
        "mean_pairwise_jaccard": round(sum(_jaccard(flagged[a], flagged[b]) for a, b in pairs) / len(pairs), 3) if pairs else 1.0,
        "risk_level_agreement_on_always_flagged": round(len(same_level) / len(always), 3) if always else 1.0,
        "per_paragraph": per_paragraph,
    }


def _side_by_side(runs: List[List[Dict[str, Any]]], order: List[str], text_of: Dict[str, str]) -> str:
    """Every flagged paragraph with each run's reason and fix under it, in document order."""

    by_run = [{item["para_identifier"]: item for item in run} for run in runs]
    ever = sorted({para for run in by_run for para in run}, key=order.index)
    blocks: List[str] = []
    for para in ever:
        lines = [f"=== {para} ===", f"ORIGINAL: {text_of[para]}", ""]
        for number, run in enumerate(by_run, start=1):
            item = run.get(para)
            if item is None:
                lines.append(f"[run {number}] not flagged")
            else:
                lines.extend([f"[run {number}] {item['risk_level']} — {item['clause']}", f"  REASON: {item['reason']}", f"  FIX:    {item['suggested_fix']}"])
            lines.append("")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


async def _run(args: argparse.Namespace) -> None:
    from src.core import container as container_module
    from src.schemas.general_review import GeneralReviewRequest
    from src.tools import general_review as review_module

    if args.clause_search:
        from src.services.clause_mapping.qdrant_search import QdrantClauseSearch

        container_module.container.clause_search = QdrantClauseSearch()

    request = GeneralReviewRequest.model_validate(json.loads(Path(args.fixture).read_text(encoding="utf-8")))
    order = [para.paraindetifier for para in request.textinformation]
    text_of = {para.paraindetifier: para.text for para in request.textinformation}

    runs: List[List[Dict[str, Any]]] = []
    traces: List[List[Dict[str, Any]]] = []
    traced = getattr(review_module, "review_with_trace", None)
    for number in range(1, args.runs + 1):
        _clear_caches(review_module)
        if traced is not None:
            response, trace = await traced(request, f"eval-run-{number}")
            traces.append(trace)
        else:
            response = await review_module.general_review_service(request, f"eval-run-{number}")
        runs.append([suggestion.model_dump() for suggestion in response.suggestions])
        print(f"run {number}: {len(runs[-1])} suggestion(s)", flush=True)

    summary = summarise(runs, order, traces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "runs.json").write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if traces:
        (out_dir / "traces.json").write_text(json.dumps(traces, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "fixes_side_by_side.txt").write_text(_side_by_side(runs, order, text_of), encoding="utf-8")

    print(json.dumps({key: value for key, value in summary.items() if key != "per_paragraph"}, indent=2))
    for para, row in summary["per_paragraph"].items():
        points = f"  points {row['points']}" if "points" in row else ""
        print(f"  {para:>10}  flagged {row['flagged_in']}/{summary['runs']}  {row['risk_levels']}{points}")
    print(f"\nWritten to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="A general review request body as JSON.")
    parser.add_argument("--runs", type=int, default=5, help="Cold runs to compare.")
    parser.add_argument("--out", default=str(ROOT / "eval_output" / "general_review"), help="Directory for runs.json, summary.json and fixes_side_by_side.txt.")
    parser.add_argument("--summarise", metavar="RUNS_JSON", help="Skip the review and print the figures for a runs.json written by an earlier run.")
    parser.add_argument("--clause-search", action="store_true", help="Map clause types through the Qdrant vector DB as production does.")
    args = parser.parse_args()
    if args.summarise:
        request = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        order = [para["paraindetifier"] for para in request["textinformation"]]
        summary = summarise(json.loads(Path(args.summarise).read_text(encoding="utf-8")), order)
        print(json.dumps({key: value for key, value in summary.items() if key != "per_paragraph"}, indent=2))
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
