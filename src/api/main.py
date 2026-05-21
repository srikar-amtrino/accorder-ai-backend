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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bedrock Streaming · Claude</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg-base: #07080d;
    --bg-card: rgba(255,255,255,0.025);
    --bg-input: rgba(0,0,0,0.35);
    --border: rgba(255,255,255,0.07);
    --border-strong: rgba(255,255,255,0.14);
    --text: #f4f5fa;
    --text-dim: #9ca3af;
    --text-dimmer: #6b7280;
    --accent: #6366f1;
    --accent-2: #8b5cf6;
    --success: #34d399;
    --danger: #f87171;
    --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg-base);
    color: var(--text);
    line-height: 1.55;
    min-height: 100vh;
    overflow-x: hidden;
    background-image:
      radial-gradient(at 22% -5%, rgba(99, 102, 241, 0.18) 0px, transparent 50%),
      radial-gradient(at 78% 25%, rgba(139, 92, 246, 0.10) 0px, transparent 50%),
      radial-gradient(at 50% 100%, rgba(99, 102, 241, 0.06) 0px, transparent 50%);
    background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  ::selection { background: rgba(99, 102, 241, 0.4); color: white; }
  .container { max-width: 940px; margin: 0 auto; padding: 56px 24px 80px; }

  /* Hero */
  .hero { margin-bottom: 40px; }
  .hero-tag {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px;
    background: rgba(99, 102, 241, 0.12);
    border: 1px solid rgba(99, 102, 241, 0.25);
    border-radius: 999px;
    font-size: 12px; color: #c7d2fe;
    margin-bottom: 22px; font-weight: 500; letter-spacing: 0.02em;
  }
  .hero-tag .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--success); box-shadow: 0 0 10px var(--success);
    animation: heartbeat 2s ease-in-out infinite;
  }
  @keyframes heartbeat { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .hero h1 {
    font-size: 44px; font-weight: 700; margin: 0 0 14px;
    line-height: 1.1; letter-spacing: -0.025em;
    background: linear-gradient(120deg, #f4f5fa 25%, #c7d2fe 60%, #ddd6fe 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .hero .lede {
    font-size: 16px; color: var(--text-dim);
    max-width: 620px; margin: 0;
  }

  /* Card */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    backdrop-filter: blur(20px) saturate(140%);
    -webkit-backdrop-filter: blur(20px) saturate(140%);
    box-shadow: 0 1px 0 rgba(255,255,255,0.03) inset, 0 24px 60px -28px rgba(0,0,0,0.6);
  }
  .card + .card { margin-top: 18px; }

  /* Example chips */
  .examples {
    display: flex; flex-wrap: wrap; gap: 8px;
    margin-bottom: 20px;
  }
  .chip {
    background: rgba(255,255,255,0.035);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 7px 14px;
    border-radius: 999px;
    font-size: 13px; font-weight: 500;
    cursor: pointer; transition: all 0.18s ease;
    font-family: inherit;
  }
  .chip:hover {
    background: rgba(99, 102, 241, 0.1);
    border-color: rgba(99, 102, 241, 0.4);
    color: var(--text); transform: translateY(-1px);
  }

  /* Prompt label + textarea */
  .prompt-label {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px;
  }
  .prompt-label .label {
    font-size: 11px; color: var(--text-dim); font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
  }
  .prompt-label .hint {
    font-size: 11px; color: var(--text-dimmer);
  }
  .prompt-label .hint kbd {
    font-family: var(--mono); font-size: 10px;
    background: rgba(255,255,255,0.06);
    padding: 2px 6px; border-radius: 4px;
    border: 1px solid var(--border);
  }
  textarea {
    width: 100%; min-height: 100px;
    padding: 14px 16px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 12px;
    color: var(--text);
    font: inherit; font-size: 15px; line-height: 1.55;
    resize: vertical;
    transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
  }
  textarea::placeholder { color: var(--text-dimmer); }
  textarea:focus {
    outline: none;
    border-color: rgba(99, 102, 241, 0.5);
    background: rgba(0,0,0,0.45);
    box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.12);
  }

  /* Controls row */
  .controls {
    display: flex; flex-wrap: wrap; gap: 12px;
    align-items: center; margin-top: 16px;
  }
  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 18px; border: none; border-radius: 10px;
    font: inherit; font-size: 14px; font-weight: 500;
    cursor: pointer; transition: all 0.18s ease;
  }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
    color: white;
    box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4), 0 0 0 1px rgba(255,255,255,0.08) inset;
  }
  .btn-primary:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 6px 22px rgba(99, 102, 241, 0.55), 0 0 0 1px rgba(255,255,255,0.12) inset;
  }
  .btn-primary:disabled {
    background: rgba(255,255,255,0.05); color: var(--text-dimmer);
    box-shadow: none; cursor: not-allowed;
  }
  .btn-secondary {
    background: rgba(255,255,255,0.05);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.12);
    border-color: rgba(248, 113, 113, 0.35);
    color: var(--danger);
  }
  .btn-secondary:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Pace segment control */
  .pace { margin-left: auto; display: flex; align-items: center; gap: 10px; }
  .pace-label {
    font-size: 11px; color: var(--text-dim); font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
  }
  .segment {
    display: inline-flex;
    background: rgba(0,0,0,0.35);
    border: 1px solid var(--border);
    border-radius: 10px; padding: 3px; gap: 2px;
  }
  .segment button {
    background: transparent; border: none;
    color: var(--text-dim);
    padding: 7px 13px; border-radius: 7px;
    font: inherit; font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all 0.15s;
  }
  .segment button:hover:not(:disabled):not(.active) { color: var(--text); background: rgba(255,255,255,0.04); }
  .segment button.active {
    background: linear-gradient(135deg, rgba(99,102,241,0.25) 0%, rgba(139,92,246,0.25) 100%);
    color: #e0e7ff; box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.3);
  }
  .segment button:disabled { cursor: not-allowed; opacity: 0.5; }

  /* Stats */
  .stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
    margin-bottom: 18px;
  }
  .stat {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px;
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    transition: border-color 0.15s;
  }
  .stat.active { border-color: rgba(99, 102, 241, 0.3); }
  .stat .label {
    display: flex; align-items: center; gap: 7px;
    font-size: 10.5px; color: var(--text-dimmer); font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px;
  }
  .stat .label svg { width: 13px; height: 13px; opacity: 0.7; }
  .stat .value {
    font-size: 22px; font-weight: 600; color: var(--text);
    font-variant-numeric: tabular-nums; letter-spacing: -0.015em;
    font-family: var(--mono);
  }
  .stat .value .unit {
    font-size: 12px; color: var(--text-dimmer); font-weight: 400;
    margin-left: 5px; font-family: "Inter", sans-serif;
  }

  /* Response */
  .response-card { position: relative; }
  .response-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 18px; padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .response-title {
    display: flex; align-items: center; gap: 10px;
    font-size: 11px; font-weight: 600; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.08em;
  }
  .response-title svg { width: 14px; height: 14px; }
  .live-pill {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 11px;
    background: rgba(52, 211, 153, 0.1);
    border: 1px solid rgba(52, 211, 153, 0.3);
    border-radius: 999px;
    font-size: 11px; color: var(--success);
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
    opacity: 0; transform: translateY(-2px);
    transition: opacity 0.2s, transform 0.2s;
  }
  .live-pill.show { opacity: 1; transform: translateY(0); }
  .live-pill .pulse {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--success); box-shadow: 0 0 6px var(--success);
    animation: pulse 1.2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.85); }
  }
  .response-body {
    min-height: 260px;
    font-size: 15.5px; line-height: 1.75;
    white-space: pre-wrap; word-wrap: break-word;
    color: var(--text);
  }
  .response-body.empty {
    color: var(--text-dimmer);
    display: flex; align-items: center; justify-content: center;
    text-align: center; font-size: 14px;
  }
  .response-body.empty .placeholder-icon {
    margin: 0 auto 14px; display: block; opacity: 0.35;
    color: var(--accent);
  }
  .caret {
    display: inline-block; width: 7px; height: 1.05em;
    vertical-align: text-bottom;
    background: linear-gradient(180deg, var(--accent) 0%, var(--accent-2) 100%);
    margin-left: 2px; border-radius: 2px;
    animation: blink 1s steps(2) infinite;
    box-shadow: 0 0 8px rgba(99, 102, 241, 0.6);
  }
  @keyframes blink { 50% { opacity: 0.15; } }

  /* Footer */
  .footer {
    margin-top: 36px; padding-top: 24px;
    border-top: 1px solid var(--border);
    display: flex; justify-content: space-between;
    font-size: 11.5px; color: var(--text-dimmer);
    flex-wrap: wrap; gap: 12px;
  }
  .footer code {
    background: rgba(255,255,255,0.05);
    padding: 2px 6px; border-radius: 4px;
    font-family: var(--mono); font-size: 11px;
    color: var(--text-dim);
  }
  .footer .brand { color: var(--text-dim); font-weight: 500; }

  /* Responsive */
  @media (max-width: 700px) {
    .container { padding: 32px 16px 56px; }
    .hero h1 { font-size: 32px; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .pace { margin-left: 0; width: 100%; }
    .pace .segment { flex: 1; }
    .pace .segment button { flex: 1; }
  }
</style>
</head>
<body>
<div class="container">

  <header class="hero">
    <div class="hero-tag">
      <span class="dot"></span>
      Bedrock · Live Streaming Demo
    </div>
    <h1>Watch Claude Think.</h1>
    <p class="lede">
      Tokens stream from AWS Bedrock straight to your browser the moment Claude generates them.
      No buffering, no waiting for the full response — this is the same streaming primitive the
      backend uses under the hood for chat-style endpoints.
    </p>
  </header>

  <div class="card">
    <div class="examples">
      <button class="chip" data-prompt="Explain what an indemnification clause does in a commercial contract, in 3 short paragraphs.">Explain indemnification</button>
      <button class="chip" data-prompt="Draft a confidentiality clause for a mutual NDA in plain modern English with 5 numbered sub-sections covering definition, exclusions, use, return of materials, and remedies.">Draft NDA confidentiality</button>
      <button class="chip" data-prompt="What is a force majeure clause and when does it apply? Give a real-world example a non-lawyer can follow.">Force majeure</button>
      <button class="chip" data-prompt="Compare arbitration vs litigation vs mediation as dispute resolution mechanisms. Cover speed, cost, privacy, and enforceability.">Arbitration vs mediation</button>
    </div>

    <div class="prompt-label">
      <span class="label">Prompt</span>
      <span class="hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> to send</span>
    </div>
    <textarea id="prompt" placeholder="Ask Claude anything…">Explain what an indemnification clause does in a commercial contract, in 3 short paragraphs.</textarea>

    <div class="controls">
      <button id="send" class="btn btn-primary">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 18l6-6-6-6"/></svg>
        Stream response
      </button>
      <button id="stop" class="btn btn-secondary" disabled>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        Stop
      </button>

      <div class="pace">
        <span class="pace-label">Pace</span>
        <div class="segment" id="pace">
          <button data-pace="15">Slow</button>
          <button data-pace="40" class="active">Demo</button>
          <button data-pace="100">Fast</button>
          <button data-pace="0">Raw</button>
        </div>
      </div>
    </div>
  </div>

  <div class="stats">
    <div class="stat" id="stat-status">
      <div class="label">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>
        Status
      </div>
      <div class="value" id="status">Idle</div>
    </div>
    <div class="stat">
      <div class="label">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M4 12h16M4 17h10"/></svg>
        Output
      </div>
      <div class="value"><span id="counts">0</span><span class="unit">chars</span></div>
    </div>
    <div class="stat">
      <div class="label">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
        Bedrock Rate
      </div>
      <div class="value"><span id="rate">—</span><span class="unit">ch/s</span></div>
    </div>
    <div class="stat">
      <div class="label">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
        Elapsed
      </div>
      <div class="value"><span id="elapsed">0.0</span><span class="unit">s</span></div>
    </div>
  </div>

  <div class="card response-card">
    <div class="response-header">
      <div class="response-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        Claude Response
      </div>
      <div class="live-pill" id="live-pill">
        <span class="pulse"></span>
        Streaming Live
      </div>
    </div>
    <div class="response-body empty" id="out">
      <div>
        <svg class="placeholder-icon" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        Response will stream here. Pick a prompt or click <strong style="color: var(--text-dim)">Stream response</strong>.
      </div>
    </div>
  </div>

  <footer class="footer">
    <span class="brand">Powered by <strong style="color: var(--text);">AWS Bedrock</strong> · <code>invoke_model_with_response_stream</code></span>
    <span>Gated by <code>DEBUG=true</code> · Not for production</span>
  </footer>

</div>

<script>
  const $ = (id) => document.getElementById(id);
  let controller = null;
  let currentPace = 40;
  let cancelled = false;
  let drainResolve = null;
  let drainComplete = null;

  // Pace segment control
  $('pace').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-pace]');
    if (!btn || btn.disabled) return;
    $('pace').querySelectorAll('button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentPace = parseInt(btn.dataset.pace, 10);
  });

  // Example chips
  document.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      $('prompt').value = chip.dataset.prompt;
      $('prompt').focus();
    });
  });

  function setStatus(text, accent) {
    $('status').textContent = text;
    $('stat-status').classList.toggle('active', !!accent);
  }

  function teardownStream() {
    // Run in finally — leaves UI in idle-but-readable state.
    $('send').disabled = false;
    $('stop').disabled = true;
    $('pace').querySelectorAll('button').forEach(b => b.disabled = false);
    $('live-pill').classList.remove('show');
    const caret = document.getElementById('caret');
    if (caret) caret.remove();
    controller = null;
  }

  async function startStream() {
    const prompt = $('prompt').value.trim();
    if (!prompt) return;

    cancelled = false;
    $('send').disabled = true;
    $('stop').disabled = false;
    $('pace').querySelectorAll('button').forEach(b => b.disabled = true);

    // Reset UI
    $('counts').textContent = '0';
    $('rate').textContent = '—';
    $('elapsed').textContent = '0.0';
    setStatus('Connecting', true);
    $('live-pill').classList.add('show');

    const msPerChar = currentPace > 0 ? Math.max(8, Math.round(1000 / currentPace)) : 0;
    const start = performance.now();
    let charsIngested = 0;

    const tick = setInterval(() => {
      const sec = (performance.now() - start) / 1000;
      $('elapsed').textContent = sec.toFixed(1);
      if (sec > 0.1) $('rate').textContent = (charsIngested / sec).toFixed(0);
    }, 100);

    controller = new AbortController();

    // Render buffer + drain loop
    let buffer = '';
    let streamDone = false;
    drainComplete = new Promise(r => { drainResolve = r; });
    const out = $('out');
    out.classList.remove('empty');
    out.innerHTML = '<span class="caret" id="caret"></span>';
    const caret = $('caret');

    function drainOnce() {
      // Cancel point: stop draining immediately if user aborted.
      if (cancelled) { drainResolve && drainResolve(); return; }
      if (buffer.length === 0) {
        if (streamDone) { drainResolve && drainResolve(); }
        else setTimeout(drainOnce, 20);
        return;
      }
      if (msPerChar === 0) {
        // Raw — flush everything arrived this frame.
        caret.insertAdjacentText('beforebegin', buffer);
        buffer = '';
        requestAnimationFrame(drainOnce);
      } else {
        const ch = buffer.charAt(0);
        buffer = buffer.slice(1);
        caret.insertAdjacentText('beforebegin', ch);
        setTimeout(drainOnce, msPerChar);
      }
    }
    drainOnce();

    try {
      const res = await fetch('/demo/stream?prompt=' + encodeURIComponent(prompt), { signal: controller.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      setStatus('Streaming', true);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        if (cancelled) { try { await reader.cancel(); } catch (_) {} break; }
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        if (!chunk) continue;
        charsIngested += chunk.length;
        buffer += chunk;
        $('counts').textContent = charsIngested.toLocaleString();
      }
      streamDone = true;
      await drainComplete;
      if (cancelled) {
        setStatus('Stopped', false);
      } else {
        const sec = ((performance.now() - start) / 1000).toFixed(1);
        setStatus('Done in ' + sec + 's', false);
      }
    } catch (err) {
      streamDone = true;
      cancelled = true;
      // Make sure drain promise resolves so teardown runs.
      if (drainResolve) drainResolve();
      if (err.name === 'AbortError') setStatus('Stopped', false);
      else setStatus('Error', false);
    } finally {
      clearInterval(tick);
      teardownStream();
    }
  }

  $('send').addEventListener('click', startStream);
  $('stop').addEventListener('click', () => {
    // Mark cancelled BEFORE aborting so the drain loop sees the flag
    // on its next tick (otherwise it keeps typing out the buffer).
    cancelled = true;
    if (controller) controller.abort();
    if (drainResolve) drainResolve();
  });
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
