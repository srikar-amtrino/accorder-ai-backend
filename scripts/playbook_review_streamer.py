import json
import asyncio
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from src.core.container import get_bedrock_model
from src.tools.playbook_review import extract_clauses_from_paragraphs, _build_reviewed_rules_summary

app = FastAPI()

SIMILARITY_SYSTEM_PROMPT = Path("src/services/prompts/v1/ai_review_system.mustache").read_text(encoding="utf-8")
SIMILARITY_USER_PROMPT = Path("src/services/prompts/v1/ai_review_user.mustache").read_text(encoding="utf-8")
MISSING_CLAUSES_PROMPT = Path("src/services/prompts/v1/missing_clauses.mustache").read_text(encoding="utf-8")

bedrock_model = get_bedrock_model()


class _Para:
    def __init__(self, d: Dict[str, Any]):
        self.text = d.get("text", "")
        # keep server spelling
        self.paraindetifier = d.get("paraindetifier", d.get("para_identifier", ""))


@app.post("/stream-playbook")
async def stream_playbook(request: Request):
    payload = await request.json()
    session_id = payload.get("session_id", "")
    rules = payload.get("rulesinformation", [])
    textinformation = payload.get("textinformation", [])

    paras = [_Para(p) for p in textinformation]
    unique_titles = list(dict.fromkeys(rule.get("title") for rule in rules))
    clause_map = extract_clauses_from_paragraphs(paras, unique_titles, session_id=session_id)

    async def event_stream():
        # Stream per-rule evaluation
        for idx, rule in enumerate(rules, start=1):
            rule_title = rule.get("title")
            rule_type = rule.get("rule_type") or rule.get("type") or "primary"
            matched_paras = clause_map.get(rule_title, [])

            if matched_paras:
                paragraph_context = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text.strip()}" for p in matched_paras)
            else:
                paragraph_context = ""

            context = {
                "rule_title": rule_title,
                "rule_instruction": rule.get("instruction", ""),
                "rule_description": rule.get("description", ""),
                "paragraphs": paragraph_context,
                "rule_type": rule_type,
            }

            # notify rule start
            yield f"data: {json.dumps({'event': 'rule_start', 'index': idx, 'total': len(rules), 'rule': rule_title})}\n\n"

            # stream chunks from bedrock
            try:
                async for chunk in bedrock_model.generate_stream(
                    prompt=SIMILARITY_USER_PROMPT, context=context, session_id=session_id, system_message=SIMILARITY_SYSTEM_PROMPT
                ):
                    payload_chunk = {"event": "rule_chunk", "rule": rule_title, "text": chunk}
                    yield f"data: {json.dumps(payload_chunk)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'event': 'rule_error', 'rule': rule_title, 'error': str(exc)})}\n\n"

            # notify rule done
            yield f"data: {json.dumps({'event': 'rule_done', 'rule': rule_title})}\n\n"

        # After all rules, request missing clauses
        full_text = "\n\n".join(f"PARA_ID: {p.paraindetifier}\nTEXT: {p.text}" for p in paras)
        reviewed_rules_summary = _build_reviewed_rules_summary({(r.get('title'), r.get('rule_type') or r.get('type') or 'primary'): {'content': type('X', (), {'para_identifiers': ','.join([p.paraindetifier for p in clause_map.get(r.get('title'), [])]), 'status': 'unknown'})} for r in rules})

        yield f"data: {json.dumps({'event': 'missing_start'})}\n\n"

        missing_context = {"data": full_text, "reviewed_rules_summary": reviewed_rules_summary}
        try:
            async for chunk in bedrock_model.generate_stream(
                prompt=MISSING_CLAUSES_PROMPT, context=missing_context, session_id=session_id
            ):
                yield f"data: {json.dumps({'event': 'missing_chunk', 'text': chunk})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'event': 'missing_error', 'error': str(exc)})}\n\n"

        yield f"data: {json.dumps({'event': 'done'})}\n\n"

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "Access-Control-Allow-Origin": "*"}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9001)
