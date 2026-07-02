"""Two-call Contract Analyzer.

Call 1 returns summary/key_information/timeline (streamed live for instant feedback).
Call 2 returns the risk list with a self-verify pass, grounded by the clause index,
and is spliced into the same JSON object after a short natural pause.

Container-free (model passed in) so it is testable directly.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from src.schemas.contract_analyzer import ContractAnalyzerResponse, ContractSectionsResponse, RiskOnlyResponse
from src.services.clause_index import build_clause_index

_V3 = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v3"
_SECTIONS_SYSTEM = (_V3 / "contract_analyzer_2call" / "sections_system.mustache").read_text(encoding="utf-8")
_SECTIONS_USER = (_V3 / "contract_analyzer_2call" / "sections_user.mustache").read_text(encoding="utf-8")
_RISK_SYSTEM = (_V3 / "contract_analyzer" / "system.mustache").read_text(encoding="utf-8")
_RISK_USER = (_V3 / "contract_analyzer_2call" / "risk_user.mustache").read_text(encoding="utf-8")

_STREAM_CHUNK_CHARS = 48


async def _sections(model: Any, content: str, session_id: str) -> ContractSectionsResponse:
    return await model.generate(
        prompt=_SECTIONS_USER,
        context={"contract_text": content},
        response_model=ContractSectionsResponse,
        session_id=session_id,
        system_message=_SECTIONS_SYSTEM,
        temperature=0.0,
    )


async def _risk(model: Any, content: str, clause_index: str, session_id: str) -> RiskOnlyResponse:
    return await model.generate(
        prompt=_RISK_USER,
        context={"contract_text": content, "clause_index": clause_index},
        response_model=RiskOnlyResponse,
        session_id=session_id,
        system_message=_RISK_SYSTEM,
        temperature=0.0,
    )


async def analyze_contract_2call(model: Any, content: str, session_id: str) -> Tuple[ContractAnalyzerResponse, Dict[str, float]]:
    """Sequential sections -> risk, merged into one response. Returns (response, timings)."""
    clause_index = build_clause_index(content)
    t0 = time.time()
    sections = await _sections(model, content, session_id)
    t1 = time.time()
    risk = await _risk(model, content, clause_index, session_id)
    t2 = time.time()

    response = ContractAnalyzerResponse(
        summary=sections.summary,
        key_information=sections.key_information,
        timeline_and_key_milestones=sections.timeline_and_key_milestones,
        risk_and_compliance_insights=risk.risk_and_compliance_insights,
    )
    return response, {"sections_s": t1 - t0, "risk_s": t2 - t1, "total_s": t2 - t0}


def get_key_information_stream(model: Any, content: str, session_id: str) -> Any:
    """Stream sections live, then splice the verified risk list into one JSON object."""

    async def event_stream() -> Any:
        clause_index = build_clause_index(content)

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

        # Phase 2 (the natural pause): run + self-verify the risk list, splice it in.
        risk = await _risk(model, content, clause_index, session_id)
        risk_json = json.dumps([r.model_dump() for r in risk.risk_and_compliance_insights])
        tail = f',"risk_and_compliance_insights":{risk_json}}}'
        for start in range(0, len(tail), _STREAM_CHUNK_CHARS):
            yield f"data: {json.dumps(tail[start : start + _STREAM_CHUNK_CHARS])}\n\n"
            await asyncio.sleep(0)
        yield "data: [DONE]\n\n"

    return event_stream()
