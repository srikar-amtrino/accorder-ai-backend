import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.context import (
    clear_context,
    set_document_id,
    set_request_id,
    set_session_id,
)
from src.api.endpoints.admin.router import router as admin_router
from src.api.endpoints.agents.main import router as agents_router
from src.api.endpoints.clause_extraction.router import (
    router as clause_extraction_router,
)
from src.api.endpoints.describe_draft.router import router as describe_draft_router
from src.api.endpoints.ingestion.router import router as ingestion_router
from src.config.logging import setup_logging
from src.config.settings import get_settings
from src.dependencies import initialize_dependencies, shutdown_dependencies

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await initialize_dependencies()
    yield
    # Shutdown
    await shutdown_dependencies()


app = FastAPI(
    title="Contract Review API",
    version="1.0.0",
    debug=settings.debug,
    lifespan=lifespan,
)


# CORS — must be added before other middleware so preflight responses include the headers.
_cors_origins = settings.cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time"],
)


# Add context middleware (session_id, document_id, request_id)
@app.middleware("http")
async def add_context_middleware(request: Request, call_next):
    """Extract and set context variables from request headers."""
    # Extract IDs from headers
    session_id = request.headers.get("X-Session-ID")
    document_id = request.headers.get("X-Document-ID")
    request_id = request.headers.get("X-Request-ID")

    # Set context variables (visible to all downstream calls in this request)
    set_session_id(session_id)
    set_document_id(document_id)
    set_request_id(request_id)

    try:
        response = await call_next(request)
    finally:
        # Clear context after request is done
        clear_context()

    return response


# Add request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


app.include_router(ingestion_router, prefix="/api/v1")
app.include_router(clause_extraction_router, prefix="/api/v1/clause-extraction")
app.include_router(describe_draft_router, prefix="/api/v1/describe-draft")
app.include_router(admin_router, prefix="/admin")
app.include_router(agents_router, prefix="/Accorder/agents")


# Mounted only when DEBUG=true. Lets you open the URL in a browser and watch
# Claude's response stream in token-by-token — the visual proof that the
# Bedrock streaming layer is doing what it claims.
if settings.debug:
    from fastapi.responses import HTMLResponse, StreamingResponse

    from src.services.llm.bedrock_model import BedrockModel

    _demo_bedrock = BedrockModel()

    @app.get("/demo/stream")
    async def demo_stream(
        prompt: str = "Explain what an indemnification clause does in a commercial contract, in 3 short paragraphs.",
    ):
        """Stream Claude's response token-by-token to the browser as plain text."""

        async def token_stream():
            async for token in _demo_bedrock.stream(
                prompt=prompt,
                context={},
                system_message="You are a helpful assistant. Answer the user clearly in plain English.",
            ):
                yield token

        return StreamingResponse(
            token_stream(),
            media_type="text/plain; charset=utf-8",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    _DEMO_STREAM_UI_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Bedrock Claude — Streaming Demo</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", system-ui, sans-serif;
         max-width: 820px; margin: 40px auto; padding: 0 18px;
         background: #0f1117; color: #e8eaf2; line-height: 1.5; }
  h1 { font-size: 22px; margin: 0 0 4px; color: #f5f7fb; }
  .sub { color: #8a90a4; font-size: 13px; margin-bottom: 26px; }
  label { display: block; font-size: 13px; color: #b3b9cc; margin-bottom: 6px; }
  textarea { width: 100%; min-height: 96px; padding: 12px; border-radius: 8px;
             border: 1px solid #2a2f3d; background: #181c27; color: #e8eaf2;
             font: inherit; resize: vertical; box-sizing: border-box; }
  textarea:focus { outline: none; border-color: #4f7cf7; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
  button { background: #4f7cf7; color: white; border: none; padding: 10px 22px;
           border-radius: 6px; font: inherit; cursor: pointer; transition: background .15s; }
  button:hover:not(:disabled) { background: #5a87f8; }
  button:disabled { background: #2a2f3d; color: #6a7184; cursor: not-allowed; }
  button.secondary { background: #2a2f3d; }
  button.secondary:hover:not(:disabled) { background: #353b4d; }
  .meta { color: #8a90a4; font-size: 12px; margin: 18px 0 8px;
          display: flex; gap: 22px; flex-wrap: wrap; }
  .meta b { color: #c8cee0; font-weight: 600; }
  .response { background: #181c27; border: 1px solid #2a2f3d; border-radius: 8px;
              padding: 20px; min-height: 220px; white-space: pre-wrap;
              line-height: 1.6; font-size: 15px; }
  .caret { display: inline-block; width: 8px; background: #4f7cf7;
           height: 1.15em; vertical-align: text-bottom;
           animation: blink 1s steps(2) infinite; margin-left: 2px; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px;
           font-size: 11px; background: #2a2f3d; color: #b3b9cc; margin-left: 8px; }
  .badge.live { background: #1e3a5f; color: #5a87f8; }
  .footer { color: #5a6075; font-size: 11px; margin-top: 22px; text-align: center; }
  @keyframes blink { 50% { opacity: 0; } }
</style>
</head>
<body>
  <h1>Bedrock Claude — Live Streaming Demo
    <span class=\"badge\" id=\"badge\">idle</span>
  </h1>
  <div class=\"sub\">
    Tokens arrive from Bedrock as Claude generates them — no buffering, no full-response wait.
    This page calls <code>/demo/stream</code> and renders each chunk as it lands.
  </div>

  <label for=\"prompt\">Prompt</label>
  <textarea id=\"prompt\">Explain what an indemnification clause does in a commercial contract, in 3 short paragraphs.</textarea>

  <div class=\"row\">
    <button id=\"send\">Stream response</button>
    <button id=\"stop\" class=\"secondary\" disabled>Stop</button>
  </div>

  <div class=\"meta\">
    <span>Status: <b id=\"status\">Idle</b></span>
    <span>Output: <b id=\"counts\">0 chars</b></span>
    <span>Rate: <b id=\"rate\">— ch/s</b></span>
    <span>Elapsed: <b id=\"elapsed\">0.0 s</b></span>
  </div>

  <div class=\"response\" id=\"out\">Click <b>Stream response</b> above to start.</div>

  <div class=\"footer\">Powered by Bedrock <code>invoke_model_with_response_stream</code> · gated by <code>DEBUG=true</code></div>

<script>
const $ = (id) => document.getElementById(id);
let controller = null;

async function startStream() {
  const prompt = $('prompt').value.trim();
  if (!prompt) return;
  $('send').disabled = true;
  $('stop').disabled = false;
  $('out').textContent = '';
  $('counts').textContent = '0 chars';
  $('rate').textContent = '— ch/s';
  $('elapsed').textContent = '0.0 s';
  $('status').textContent = 'Connecting…';
  $('badge').textContent = 'streaming';
  $('badge').classList.add('live');

  const start = performance.now();
  let chars = 0;
  const tick = setInterval(() => {
    const sec = (performance.now() - start) / 1000;
    $('elapsed').textContent = sec.toFixed(1) + ' s';
    if (sec > 0.1) $('rate').textContent = (chars / sec).toFixed(0) + ' ch/s';
  }, 100);

  controller = new AbortController();
  try {
    const res = await fetch('/demo/stream?prompt=' + encodeURIComponent(prompt), { signal: controller.signal });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    $('status').textContent = 'Streaming…';

    const out = $('out');
    out.innerHTML = '<span class=\"caret\" id=\"caret\"></span>';
    const caret = $('caret');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      if (!chunk) continue;
      chars += chunk.length;
      caret.insertAdjacentText('beforebegin', chunk);
      $('counts').textContent = chars + ' chars';
    }
    caret.remove();
    const sec = ((performance.now() - start) / 1000).toFixed(1);
    $('status').textContent = 'Done in ' + sec + ' s';
    $('badge').textContent = 'done';
    $('badge').classList.remove('live');
  } catch (err) {
    if (err.name === 'AbortError') {
      $('status').textContent = 'Stopped by user';
      $('badge').textContent = 'stopped';
    } else {
      $('status').textContent = 'Error: ' + err.message;
      $('badge').textContent = 'error';
    }
    $('badge').classList.remove('live');
  } finally {
    clearInterval(tick);
    $('send').disabled = false;
    $('stop').disabled = true;
    controller = null;
  }
}

$('send').addEventListener('click', startStream);
$('stop').addEventListener('click', () => controller && controller.abort());
$('prompt').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') startStream();
});
</script>
</body>
</html>"""

    @app.get("/demo/stream-ui", response_class=HTMLResponse)
    async def demo_stream_ui() -> HTMLResponse:
        """Polished demo page that streams /demo/stream and renders it word-by-word."""

        return HTMLResponse(content=_DEMO_STREAM_UI_HTML)


def main_entry() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main_entry()
