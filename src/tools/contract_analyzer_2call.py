"""Two-call Contract Analyzer.

One call returns summary/key_information/timeline and streams live. In parallel,
several independent risk analyses run against the clause index and are merged by
majority vote in code, then spliced into the same JSON object. The model instance
is passed in so the module stays container-free and directly testable.
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.schemas.contract_analyzer import (
    ContractAnalyzerResponse,
    ContractSectionsResponse,
    RiskComplianceInsight,
    RiskOnlyResponse,
)
from src.services.clause_index import build_clause_index, extract_clause_titles
from src.services.risk_consensus import build_risk_consensus

_V3 = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3"
_SECTIONS_SYSTEM = (_V3 / "contract_analyzer_2call" / "sections_system.mustache").read_text(encoding="utf-8")
_SECTIONS_USER = (_V3 / "contract_analyzer_2call" / "sections_user.mustache").read_text(encoding="utf-8")
_RISK_SYSTEM = (_V3 / "contract_analyzer" / "system.mustache").read_text(encoding="utf-8")
_RISK_USER = (_V3 / "contract_analyzer_2call" / "risk_user.mustache").read_text(encoding="utf-8")

_STREAM_CHUNK_CHARS = 48

# Number of independent risk analyses merged by majority vote. They run in
# parallel, so latency equals the slowest single call.
RISK_VOTES = 5


def _norm_title(title: str) -> str:
    """Normalise a clause title for matching (mirror of risk_consensus._norm)."""
    return re.sub(r"\s+", " ", (title or "").strip().strip(".").strip()).lower()


def build_para_map(textinformation: List[Any]) -> Dict[str, str]:
    """Map each clause title to the input paragraph identifier it came from.

    Grounding para_identifier in code (not via the model) keeps it deterministic
    and guarantees the caller's own paragraph id (e.g. "P0026") instead of a
    section number the model infers from the contract's own text.
    """
    para_map: Dict[str, str] = {}
    for para in textinformation:
        for title in extract_clause_titles(para.text):
            key = _norm_title(title)
            if key and key not in para_map:
                para_map[key] = para.paraindetifier
    return para_map


def _apply_para_ids(risks: List[RiskComplianceInsight], para_map: Optional[Dict[str, str]]) -> None:
    """Overwrite each risk's para_identifier with the grounded input paragraph id."""
    if not para_map:
        return
    for r in risks:
        pid = para_map.get(_norm_title(r.clause_title))
        if pid:
            r.para_identifier = pid


async def _sections(model: Any, content: str, session_id: str) -> ContractSectionsResponse:
    return await model.generate(
        prompt=_SECTIONS_USER,
        context={"contract_text": content},
        response_model=ContractSectionsResponse,
        session_id=session_id,
        system_message=_SECTIONS_SYSTEM,
        temperature=0.0,
    )


async def _risk_one(model: Any, content: str, clause_index: str, session_id: str) -> RiskOnlyResponse:
    return await model.generate(
        prompt=_RISK_USER,
        context={"contract_text": content, "clause_index": clause_index},
        response_model=RiskOnlyResponse,
        session_id=session_id,
        system_message=_RISK_SYSTEM,
        temperature=0.0,
    )


async def _risk_consensus(model: Any, content: str, session_id: str) -> List[RiskComplianceInsight]:
    """N parallel risk votes -> grounded majority-vote consensus list."""
    clause_index = build_clause_index(content)
    results = await asyncio.gather(
        *[_risk_one(model, content, clause_index, session_id) for _ in range(RISK_VOTES)],
        return_exceptions=True,
    )
    risk_lists = [r.risk_and_compliance_insights for r in results if isinstance(r, RiskOnlyResponse)]
    if not risk_lists:
        for r in results:
            if isinstance(r, Exception):
                raise r
        raise RuntimeError("Risk analysis produced no responses")
    return build_risk_consensus(risk_lists, extract_clause_titles(content))


async def analyze_contract_2call(
    model: Any, content: str, session_id: str, para_map: Optional[Dict[str, str]] = None
) -> Tuple[ContractAnalyzerResponse, Dict[str, float]]:
    """Sections and risk consensus run concurrently, merged into one response."""
    t0 = time.time()
    sections, risks = await asyncio.gather(
        _sections(model, content, session_id),
        _risk_consensus(model, content, session_id),
    )
    total = time.time() - t0

    _apply_para_ids(risks, para_map)
    response = ContractAnalyzerResponse(
        summary=sections.summary,
        key_information=sections.key_information,
        timeline_and_key_milestones=sections.timeline_and_key_milestones,
        risk_and_compliance_insights=risks,
    )
    return response, {"total_s": total}


def get_key_information_stream(model: Any, content: str, session_id: str, para_map: Optional[Dict[str, str]] = None) -> Any:
    """Stream sections live while the risk consensus runs; splice into one JSON object."""

    async def event_stream() -> Any:
        # Kick off the risk votes first so they overlap the sections stream.
        risk_task = asyncio.create_task(_risk_consensus(model, content, session_id))

        try:
            # Phase 1: stream sections live with a small lag, so we can withhold the
            # object's closing brace (replaced by the risk list) without buffering.
            buf = ""
            emitted = 0
            lag = 6
            async for chunk in model.generate_stream(
                prompt=_SECTIONS_USER, context={"contract_text": content}, session_id=session_id, temperature=0.0, system_message=_SECTIONS_SYSTEM
            ):
                buf += chunk
                safe = len(buf) - lag
                if safe > emitted:
                    yield f"data: {json.dumps(buf[emitted:safe])}\n\n"
                    emitted = safe

            # Emit the withheld tail up to (not including) the final closing brace.
            remainder = buf[emitted:]
            idx = remainder.rfind("}")
            pre = remainder[:idx] if idx != -1 else remainder
            if pre:
                yield f"data: {json.dumps(pre)}\n\n"

            # Phase 2: the consensus risk list, usually already done or nearly done.
            risks = await risk_task
            _apply_para_ids(risks, para_map)
        except BaseException:
            risk_task.cancel()
            raise

        risk_json = json.dumps([r.model_dump() for r in risks])
        tail = f',"risk_and_compliance_insights":{risk_json}}}'
        for start in range(0, len(tail), _STREAM_CHUNK_CHARS):
            yield f"data: {json.dumps(tail[start : start + _STREAM_CHUNK_CHARS])}\n\n"
            await asyncio.sleep(0)
        yield "data: [DONE]\n\n"

    return event_stream()
