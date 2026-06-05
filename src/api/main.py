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
from src.config.logging import get_logger, setup_logging
from src.config.settings import get_settings
from src.dependencies import initialize_dependencies, shutdown_dependencies

setup_logging()
settings = get_settings()
timing_logger = get_logger("Timing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await initialize_dependencies()
    yield
    # Shutdown
    await shutdown_dependencies()


app = FastAPI(
    title="Accorder AI BackEnd API",
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
    # Real-time, human-readable per-request timing so anyone tailing the terminal/logs
    # can see exactly how long each agent took, e.g.:
    #   [TIMING] POST /Accorder/agents/contract-analyzer -> 200 in 39.42s
    timing_logger.info(
        f"[TIMING] {request.method} {request.url.path} -> {response.status_code} "
        f"in {process_time:.2f}s"
    )
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
    /* Sapphire Aurora palette — deep navy base with cyan→emerald→amber accents.
       Cyan #06b6d4 and emerald #10b981 lead; amber #fbbf24 highlights the
       "wow" metric (Bedrock rate). All text colors checked for ≥4.5:1 contrast
       against bg-base, all interactive controls ≥3:1 against their surface. */
    --bg-base: #050912;
    --bg-card: rgba(255, 255, 255, 0.028);
    --bg-card-hover: rgba(255, 255, 255, 0.045);
    --bg-input: rgba(2, 6, 18, 0.55);
    --border: rgba(125, 211, 252, 0.09);
    --border-strong: rgba(125, 211, 252, 0.22);
    --text: #f1f5f9;
    --text-dim: #94a3b8;
    --text-dimmer: #64748b;
    --accent: #06b6d4;            /* cyan-500 — primary */
    --accent-2: #10b981;           /* emerald-500 — secondary */
    --accent-3: #fbbf24;           /* amber-400 — premium highlight */
    --success: #10b981;
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
      radial-gradient(at 18% -8%, rgba(6, 182, 212, 0.22) 0px, transparent 55%),
      radial-gradient(at 82% 18%, rgba(16, 185, 129, 0.16) 0px, transparent 55%),
      radial-gradient(at 50% 105%, rgba(251, 191, 36, 0.10) 0px, transparent 55%);
    background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  ::selection { background: rgba(6, 182, 212, 0.45); color: white; }
  .container { max-width: 940px; margin: 0 auto; padding: 56px 24px 80px; }

  /* Hero */
  .hero { margin-bottom: 40px; }
  .hero-tag {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px;
    background: rgba(6, 182, 212, 0.10);
    border: 1px solid rgba(6, 182, 212, 0.28);
    border-radius: 999px;
    font-size: 12px; color: #a5f3fc;
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
    background: linear-gradient(110deg, #f1f5f9 15%, #a5f3fc 45%, #6ee7b7 75%, #fcd34d 100%);
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
    background: rgba(6, 182, 212, 0.10);
    border-color: rgba(6, 182, 212, 0.4);
    color: var(--text); transform: translateY(-1px);
  }
  .chip .chip-tag {
    display: inline-block; margin-right: 7px;
    font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #6ee7b7;
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
    border-color: rgba(6, 182, 212, 0.55);
    background: rgba(2, 6, 18, 0.7);
    box-shadow: 0 0 0 4px rgba(6, 182, 212, 0.14);
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
    color: #042f2e;
    font-weight: 600;
    box-shadow: 0 4px 14px rgba(6, 182, 212, 0.35), 0 0 0 1px rgba(255,255,255,0.12) inset;
  }
  .btn-primary:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 6px 24px rgba(16, 185, 129, 0.45), 0 0 0 1px rgba(255,255,255,0.18) inset;
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
    background: linear-gradient(135deg, rgba(6, 182, 212, 0.28) 0%, rgba(16, 185, 129, 0.28) 100%);
    color: #cffafe; box-shadow: 0 0 0 1px rgba(6, 182, 212, 0.35);
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
  .stat.active { border-color: rgba(6, 182, 212, 0.35); box-shadow: 0 0 0 1px rgba(6, 182, 212, 0.15) inset; }
  .stat#stat-rate .value { color: var(--accent-3); }
  .stat#stat-rate .label svg { color: var(--accent-3); opacity: 0.9; }
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
    margin: 0 auto 14px; display: block; opacity: 0.45;
    color: var(--accent-2);
  }
  .caret {
    display: inline-block; width: 7px; height: 1.05em;
    vertical-align: text-bottom;
    background: linear-gradient(180deg, var(--accent) 0%, var(--accent-2) 100%);
    margin-left: 2px; border-radius: 2px;
    animation: blink 1s steps(2) infinite;
    box-shadow: 0 0 10px rgba(6, 182, 212, 0.7);
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
    <h1>Live Streaming Demo for Bedrock Claude opus 4.7 Model API</h1>
    <p class="lede">
      Pick one of the agent-flavored scenarios below — contract risk analysis, clause comparison, plain-English translation — and watch
      Claude's output stream from AWS Bedrock to the browser in real time. Same streaming primitive the production agents use
      under the hood; this page just renders the text instead of buffering a structured JSON response. Compare it to the
      <em style="color: var(--text-dim); font-style: normal; border-bottom: 1px dashed var(--text-dimmer);">wait-then-dump</em> UX
      a non-streaming endpoint forces on the user.
    </p>
  </header>

  <div class="card">
    <div class="examples">
      <button class="chip" data-prompt="You are a contract risk analyst. Analyze the indemnification clause below from a SaaS agreement between Acme Robotics (Customer) and Globex Software (Vendor). Walk through your reasoning out loud: identify the top 3 risks for Acme as Customer, name the party that bears each risk, quote the specific contract language causing it, and end with a concrete fix the parties could negotiate.

CLAUSE TO ANALYZE:
&quot;Customer shall defend, indemnify, and hold harmless Vendor, its affiliates, officers, employees, and agents from and against any and all claims, demands, suits, judgments, losses, damages, fines, penalties, costs, and expenses (including reasonable attorneys' fees) arising out of or relating to (a) Customer's use of the Services, (b) any breach by Customer of this Agreement, or (c) any third-party claim of any nature whatsoever in connection with Customer's business operations. Vendor's liability under this Agreement shall not exceed one hundred dollars ($100) in the aggregate, regardless of the form of action or theory of liability.&quot;"><span class="chip-tag">Agent</span>Contract Risk Analysis</button>

      <button class="chip" data-prompt="You are a contract clause comparison agent. Compare the two limitation-of-liability clauses below side by side. For each, identify: (1) the cap structure, (2) the carve-outs, (3) the consequential-damages treatment, (4) which party each version favors. End with a clear recommendation on which version a Vendor with limited insurance should prefer and why.

CLAUSE A:
&quot;In no event shall either party's aggregate liability arising out of or relating to this Agreement exceed the fees paid by Customer to Vendor in the twelve (12) months preceding the event giving rise to the claim. Neither party shall be liable for any consequential, incidental, indirect, special, punitive, or exemplary damages, including lost profits or lost data, even if advised of the possibility of such damages.&quot;

CLAUSE B:
&quot;Each party's total cumulative liability under this Agreement shall not exceed three (3) times the fees paid by Customer in the twelve (12) months preceding the claim, except that the cap shall not apply to (a) breach of confidentiality, (b) indemnification obligations, (c) gross negligence or willful misconduct, or (d) infringement of the other party's intellectual property. Consequential damages are excluded only as to lost profits and lost goodwill; lost data is recoverable as direct damages.&quot;"><span class="chip-tag">Agent</span>Clause Comparison</button>

      <button class="chip" data-prompt="You are a plain-English translator for legal contracts. Take the dense indemnification paragraph below and rewrite it as a 4-bullet plain-English summary a non-lawyer founder could understand in 30 seconds. Be faithful to the legal meaning but ruthlessly cut jargon. After the bullets, write one sentence on the practical risk this clause creates for the indemnifying party.

CLAUSE:
&quot;Notwithstanding anything to the contrary herein, Licensee shall, at its sole cost and expense, defend, indemnify, and hold harmless Licensor and its affiliates, officers, directors, employees, contractors, and agents (collectively, the &apos;Indemnified Parties&apos;) from and against any and all third-party claims, actions, suits, proceedings, losses, liabilities, damages, costs, and expenses (including reasonable attorneys&apos; fees and costs of investigation) arising out of, resulting from, or in connection with (i) Licensee&apos;s use of the Licensed Materials, (ii) any breach or alleged breach by Licensee of any representation, warranty, covenant, or obligation under this Agreement, (iii) the gross negligence or willful misconduct of Licensee or any of its personnel, or (iv) any violation by Licensee of applicable law. Licensor shall provide prompt written notice of any such claim and reasonable cooperation in the defense thereof; provided, however, that any failure or delay in providing such notice shall not relieve Licensee of its indemnification obligations except to the extent Licensee is materially prejudiced by such failure.&quot;"><span class="chip-tag">Agent</span>Plain-English Translator</button>

      <button class="chip" data-prompt="You are a senior contract drafter. Draft a complete, enforceable Confidentiality clause for a mutual NDA between two software companies who are evaluating a partnership. The clause must contain numbered sub-sections covering: (1) definition of Confidential Information with examples, (2) standard exclusions, (3) permitted use and recipients, (4) standard of care, (5) compelled disclosure procedure, (6) return or destruction on request, (7) survival period for ordinary information and a longer survival for trade secrets, (8) injunctive relief. Use plain modern English — no &apos;witnesseth&apos; or &apos;party of the first part&apos;. Use [PLACEHOLDER] tokens for facts like party names, dates, and the survival period."><span class="chip-tag">Agent</span>Draft NDA Confidentiality</button>
    </div>

    <div class="prompt-label">
      <span class="label">Prompt</span>
      <span class="hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> to send</span>
    </div>
    <textarea id="prompt" placeholder="Ask Claude anything — or click an agent scenario above to load a realistic prompt with sample contract text.">You are a contract risk analyst. The clause below appears in a SaaS agreement. In plain English, list the top 2 risks it creates for the Customer and the one phrase from the clause that creates each risk. End with a one-line suggested fix.

CLAUSE: "Customer shall pay Vendor's monthly subscription fee within five (5) days of invoice. Late payments incur a five percent (5%) per-month penalty compounded daily, and Vendor may suspend Services after ten (10) days past due without prior notice or cure period."</textarea>

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
    <div class="stat" id="stat-rate">
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

    # Realistic Word add-in mockup: the Contract Analyzer running as a docked task pane,
    # delivering its analysis over the real POST /contract-analyzer/stream SSE endpoint
    # (now a single call surfaced as one 'analysis' event). This is the "how it looks to
    # the customer inside Word" view, not a generic token demo.
    _DEMO_CONTRACT_ANALYZER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accorder AI · Contract Analyzer (Word add-in)</title>
<style>
  :root {
    --word-blue: #2b579a;
    --word-blue-d: #1e3f6f;
    --brand: #4f46e5;
    --brand-2: #7c3aed;
    --ink: #1f2329;
    --ink-dim: #5b6470;
    --ink-faint: #8a94a3;
    --line: #e3e7ec;
    --line-soft: #eef1f5;
    --canvas: #d8dde4;
    --surface: #ffffff;
    --pane: #fbfbfd;
    --crit: #dc2626; --high: #ea580c; --med: #d97706; --low: #059669;
    --ok: #16a34a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    color: var(--ink); background: var(--canvas);
    -webkit-font-smoothing: antialiased; overflow: hidden;
  }
  .word { display: flex; flex-direction: column; height: 100vh; }

  /* Title bar */
  .titlebar {
    height: 34px; flex: none; display: flex; align-items: center;
    background: var(--word-blue); color: #fff; font-size: 12.5px; padding: 0 10px; gap: 10px;
  }
  .tb-word-icon {
    width: 18px; height: 18px; border-radius: 3px; background: #fff; color: var(--word-blue);
    display: grid; place-items: center; font-weight: 800; font-size: 12px; font-family: Georgia, serif;
  }
  .tb-title { font-weight: 600; opacity: .97; }
  .tb-title .saved { opacity: .7; font-weight: 400; margin-left: 8px; }
  .tb-spacer { flex: 1; }
  .tb-acct { width: 22px; height: 22px; border-radius: 50%; background: linear-gradient(135deg,#67e8f9,#a78bfa); display: grid; place-items: center; font-size: 11px; font-weight: 700; color: #1e293b; }
  .tb-win { display: flex; gap: 0; margin-left: 6px; }
  .tb-win span { width: 30px; height: 34px; display: grid; place-items: center; font-size: 12px; opacity: .85; }
  .tb-win span:last-child:hover { background: #e81123; }

  /* Ribbon */
  .ribbon { flex: none; background: #f3f4f7; border-bottom: 1px solid var(--line); }
  .tabs { display: flex; gap: 2px; padding: 0 8px; height: 36px; align-items: stretch; font-size: 13px; }
  .tabs span { display: flex; align-items: center; padding: 0 12px; color: var(--ink-dim); cursor: default; border-bottom: 2px solid transparent; }
  .tabs .tab-active { color: var(--word-blue); font-weight: 600; border-bottom: 2px solid var(--word-blue); background: #fff; border-radius: 4px 4px 0 0; }
  .ribbon-actions { display: flex; align-items: center; gap: 6px; padding: 8px 12px; background: #fff; border-top: 1px solid var(--line-soft); }
  .rbtn { display: inline-flex; flex-direction: column; align-items: center; gap: 3px; padding: 4px 12px; border: 1px solid transparent; border-radius: 6px; background: none; cursor: pointer; font: inherit; font-size: 11px; color: var(--ink-dim); }
  .rbtn:hover { background: #eef0f6; border-color: var(--line); }
  .rbtn.primary { color: var(--brand); }
  .rbtn .ic { width: 22px; height: 22px; display: grid; place-items: center; }
  .rbtn .ic svg { width: 20px; height: 20px; }
  .rdiv { width: 1px; align-self: stretch; background: var(--line); margin: 4px 6px; }

  /* Stage: document canvas + task pane */
  .stage { flex: 1; display: flex; min-height: 0; }
  .canvas { flex: 1; overflow: auto; padding: 26px 0; display: flex; justify-content: center; }
  .page {
    background: var(--surface); width: 712px; max-width: calc(100% - 36px); min-height: 920px;
    box-shadow: 0 1px 4px rgba(0,0,0,.16), 0 10px 40px -16px rgba(0,0,0,.28);
    padding: 76px 84px; font-size: 13.5px; line-height: 1.85; color: #2a2f37;
  }
  .page h1.doc-h { font-size: 17px; text-align: center; letter-spacing: .04em; text-transform: uppercase; margin: 0 0 22px; color: #1b1f25; }
  .page .doc-h2 { font-weight: 700; margin: 18px 0 4px; color: #1b1f25; }
  .page p { margin: 0 0 11px; }
  .page .ph { color: var(--ink-faint); }
  .page-empty { display: grid; place-items: center; min-height: 760px; color: var(--ink-faint); text-align: center; }
  .page-empty svg { width: 54px; height: 54px; opacity: .5; margin-bottom: 14px; }
  .hl { background: #fff3bf; border-radius: 2px; box-shadow: 0 0 0 1px #ffe49b; transition: background .3s; }

  /* Task pane */
  .pane { flex: none; width: 390px; background: var(--pane); border-left: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
  .pane-head { padding: 14px 16px; background: linear-gradient(135deg, var(--brand), var(--brand-2)); color: #fff; }
  .ph-row { display: flex; align-items: center; gap: 10px; }
  .ph-logo { width: 30px; height: 30px; border-radius: 8px; background: rgba(255,255,255,.18); display: grid; place-items: center; font-weight: 800; font-size: 16px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.25); }
  .ph-title { font-size: 15px; font-weight: 700; line-height: 1.1; }
  .ph-title small { display: block; font-size: 11px; font-weight: 500; opacity: .85; margin-top: 2px; }
  .ph-badge { margin-left: auto; font-size: 10px; background: rgba(255,255,255,.16); padding: 3px 9px; border-radius: 999px; font-weight: 600; letter-spacing: .03em; }

  .pane-controls { padding: 12px 16px; border-bottom: 1px solid var(--line); background: #fff; }
  .file-row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
  .file-btn { flex: none; border: 1px solid var(--line); background: #f6f7f9; border-radius: 7px; padding: 8px 12px; font: inherit; font-size: 12.5px; color: var(--ink); cursor: pointer; }
  .file-btn:hover { background: #eef0f6; }
  .file-name { font-size: 12px; color: var(--ink-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .run-btn { width: 100%; border: none; border-radius: 8px; padding: 11px; font: inherit; font-size: 14px; font-weight: 600; color: #fff; cursor: pointer; background: linear-gradient(135deg, var(--brand), var(--brand-2)); box-shadow: 0 3px 10px rgba(79,70,229,.32); transition: .15s; }
  .run-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(79,70,229,.4); }
  .run-btn:disabled { background: #c7cad6; box-shadow: none; cursor: not-allowed; }
  .link-btn { width: 100%; margin-top: 8px; background: none; border: none; color: var(--ink-dim); font: inherit; font-size: 12px; cursor: pointer; text-decoration: underline dotted; }
  .link-btn:hover:not(:disabled) { color: var(--brand); }
  .link-btn:disabled { opacity: .5; cursor: not-allowed; }

  .progress { padding: 12px 16px; border-bottom: 1px solid var(--line); background: #fff; }
  .progress-top { display: flex; justify-content: space-between; align-items: center; font-size: 12.5px; margin-bottom: 8px; }
  .progress-top .st { font-weight: 600; color: var(--ink); display: flex; align-items: center; gap: 7px; }
  .progress-top .el { font-family: ui-monospace, Consolas, monospace; color: var(--ink-dim); font-size: 12px; }
  .spin { width: 12px; height: 12px; border: 2px solid #d7dae3; border-top-color: var(--brand); border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 0 rgba(22,163,74,.5); animation: ping 1.3s ease-out infinite; }
  @keyframes ping { 0% { box-shadow: 0 0 0 0 rgba(22,163,74,.45); } 70%,100% { box-shadow: 0 0 0 7px rgba(22,163,74,0); } }
  .pbar { height: 6px; background: #eceef3; border-radius: 999px; overflow: hidden; }
  .pfill { height: 100%; width: 0%; border-radius: 999px; background: linear-gradient(90deg, var(--brand), var(--brand-2)); transition: width .45s cubic-bezier(.4,0,.2,1); }
  .pmeta { display: flex; justify-content: space-between; font-size: 11px; color: var(--ink-faint); margin-top: 6px; }

  .sections { flex: 1; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 11px; }
  .sec { border: 1px solid var(--line); border-radius: 11px; background: #fff; overflow: hidden; box-shadow: 0 1px 2px rgba(16,24,40,.03); }
  .sec-head { display: flex; align-items: center; gap: 9px; padding: 11px 13px; }
  .sec-ic { width: 26px; height: 26px; border-radius: 7px; display: grid; place-items: center; flex: none; }
  .sec-ic svg { width: 15px; height: 15px; }
  .ic-sum { background: #eef2ff; color: var(--brand); }
  .ic-key { background: #ecfeff; color: #0891b2; }
  .ic-mile { background: #f0fdf4; color: #16a34a; }
  .ic-risk { background: #fef2f2; color: #dc2626; }
  .sec-title { font-size: 13.5px; font-weight: 600; flex: 1; }
  .sec-title .cnt { color: var(--ink-faint); font-weight: 500; margin-left: 6px; font-size: 12px; }
  .pill { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; padding: 3px 9px; border-radius: 999px; }
  .pill.wait { background: #f1f3f7; color: var(--ink-faint); }
  .pill.live { background: #eef2ff; color: var(--brand); }
  .pill.ok { background: #ecfdf3; color: var(--ok); }
  .pill.err { background: #fef2f2; color: var(--crit); }
  .sec-body { padding: 0 13px 13px; font-size: 13px; color: var(--ink); animation: rise .35s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .skel { height: 11px; border-radius: 5px; margin: 9px 0; background: linear-gradient(90deg,#eef1f5 25%,#e2e6ec 37%,#eef1f5 63%); background-size: 400% 100%; animation: sh 1.3s ease infinite; }
  .skel.s90{width:90%}.skel.s70{width:70%}.skel.s50{width:50%}.skel.s80{width:80%}
  @keyframes sh { 0%{background-position:100% 0} 100%{background-position:-100% 0} }
  .summary-txt { line-height: 1.65; white-space: pre-wrap; }
  .kv { display: flex; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line-soft); }
  .kv:last-child { border-bottom: none; }
  .kv .k { color: var(--ink-dim); flex: none; width: 132px; font-size: 12px; }
  .kv .v { color: var(--ink); font-weight: 500; font-size: 12.5px; }
  .mile { padding: 9px 0; border-bottom: 1px solid var(--line-soft); }
  .mile:last-child { border-bottom: none; }
  .mile .mt { font-weight: 600; font-size: 12.5px; }
  .mile .md { display: inline-block; font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: #0e7490; background: #ecfeff; padding: 1px 7px; border-radius: 5px; margin: 4px 0; }
  .mile .mb { color: var(--ink-dim); font-size: 12px; }
  .risk { border: 1px solid var(--line); border-left-width: 3px; border-radius: 8px; padding: 10px 11px; margin-bottom: 9px; }
  .risk:last-child { margin-bottom: 0; }
  .risk.Critical { border-left-color: var(--crit); } .risk.High { border-left-color: var(--high); }
  .risk.Medium { border-left-color: var(--med); } .risk.Low { border-left-color: var(--low); }
  .risk-top { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; }
  .risk-title { font-weight: 600; font-size: 12.8px; flex: 1; }
  .sev { font-size: 9.5px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; padding: 2px 7px; border-radius: 999px; flex: none; }
  .sev.Critical { background: #fef2f2; color: var(--crit); } .sev.High { background: #fff7ed; color: var(--high); }
  .sev.Medium { background: #fffbeb; color: var(--med); } .sev.Low { background: #ecfdf5; color: var(--low); }
  .risk-meta { font-size: 11px; color: var(--ink-faint); margin-bottom: 7px; }
  .risk-meta .chip { background: #f1f3f7; padding: 1px 7px; border-radius: 5px; margin-right: 5px; }
  .risk-row { font-size: 12px; color: var(--ink-dim); margin-top: 4px; line-height: 1.5; }
  .risk-row b { color: var(--ink); font-weight: 600; }
  .empty-note { color: var(--ink-faint); font-size: 12.5px; padding: 4px 0; }

  .pane-foot { flex: none; padding: 9px 16px; border-top: 1px solid var(--line); background: #fff; display: flex; align-items: center; justify-content: space-between; font-size: 11px; color: var(--ink-faint); }
  .pane-foot .pulse { display: inline-flex; align-items: center; gap: 6px; }
  input[type=file] { display: none; }
</style>
</head>
<body>
<div class="word">
  <div class="titlebar">
    <span class="tb-word-icon">W</span>
    <span class="tb-title">Contract.docx<span class="saved">— Saved to this PC</span></span>
    <span class="tb-spacer"></span>
    <span class="tb-acct">SR</span>
    <span class="tb-win"><span>&#8211;</span><span>&#9633;</span><span>&#10005;</span></span>
  </div>

  <div class="ribbon">
    <div class="tabs">
      <span>File</span><span>Home</span><span>Insert</span><span>Draw</span><span>Layout</span><span>References</span><span>Review</span><span>View</span><span class="tab-active">Accorder AI</span>
    </div>
    <div class="ribbon-actions">
      <button class="rbtn primary" id="ribbonAnalyze">
        <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M9 13l2 2 4-4"/></svg></span>
        Analyze
      </button>
      <div class="rdiv"></div>
      <button class="rbtn"><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 5h16M4 12h16M4 19h10"/></svg></span>Review</button>
      <button class="rbtn"><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 4H5v16h3M16 4h3v16h-3"/></svg></span>Compare</button>
      <button class="rbtn"><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 19l7-7-3-3-7 7v3z"/><path d="M16 9l-3-3"/></svg></span>Draft</button>
      <button class="rbtn"><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg></span>History</button>
    </div>
  </div>

  <div class="stage">
    <div class="canvas" id="canvas">
      <div class="page" id="page">
        <div class="page-empty" id="pageEmpty">
          <div>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>
            <div>Your contract appears here.<br>Choose a <b>.docx</b> in the panel &rarr; then click <b>Analyze contract</b>.</div>
          </div>
        </div>
      </div>
    </div>

    <aside class="pane">
      <div class="pane-head">
        <div class="ph-row">
          <div class="ph-logo">A</div>
          <div class="ph-title">Accorder AI<small>Contract Analyzer</small></div>
          <div class="ph-badge">LIVE</div>
        </div>
      </div>

      <div class="pane-controls">
        <div class="file-row">
          <button class="file-btn" id="fileBtn">Choose .docx</button>
          <span class="file-name" id="fileName">No file selected</span>
          <input type="file" id="file" accept=".docx">
        </div>
        <button class="run-btn" id="run" disabled>Analyze contract</button>
        <button class="link-btn" id="runClassic" disabled>Compare with classic (wait-then-dump) mode</button>
      </div>

      <div class="progress">
        <div class="progress-top">
          <span class="st" id="statusWrap"><span id="statusText">Ready when you are</span></span>
          <span class="el" id="elapsed">0.0s</span>
        </div>
        <div class="pbar"><div class="pfill" id="pfill"></div></div>
        <div class="pmeta"><span id="secCount">waiting</span><span id="firstMeta"></span></div>
      </div>

      <div class="sections" id="sections"></div>

      <div class="pane-foot">
        <span>Powered by AWS Bedrock &middot; Claude</span>
        <span class="pulse" id="footPulse"></span>
      </div>
    </aside>
  </div>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const STREAM_URL = "/Accorder/agents/contract-analyzer/stream?include_document=true";
  const CLASSIC_URL = "/Accorder/agents/contract-analyzer";
  const SESSION = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : "sess-" + Math.random().toString(16).slice(2);

  const ICONS = {
    summary: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
    keyinfo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 5h7v7H3zM14 5h7v4h-7zM14 12h7v7h-7zM3 15h7v4H3z"/></svg>',
    milestones: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    risks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/></svg>',
  };
  const CARD_DEFS = [
    { key: "summary", title: "Executive Summary", ic: "ic-sum" },
    { key: "keyinfo", title: "Key Information", ic: "ic-key" },
    { key: "milestones", title: "Timeline & Milestones", ic: "ic-mile" },
    { key: "risks", title: "Risks & Compliance", ic: "ic-risk" },
  ];

  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  function skel(n) { let s = ""; const w = ["s90", "s70", "s80", "s50", "s90"]; for (let i = 0; i < n; i++) s += '<div class="skel ' + w[i % w.length] + '"></div>'; return s; }

  function buildCards() {
    $("sections").innerHTML = CARD_DEFS.map(c => (
      '<div class="sec" id="sec-' + c.key + '">' +
        '<div class="sec-head">' +
          '<div class="sec-ic ' + c.ic + '">' + ICONS[c.key] + '</div>' +
          '<div class="sec-title">' + c.title + '<span class="cnt" id="cnt-' + c.key + '"></span></div>' +
          '<span class="pill wait" id="pill-' + c.key + '">Waiting</span>' +
        '</div>' +
        '<div class="sec-body" id="body-' + c.key + '">' + skel(c.key === "summary" ? 4 : 3) + '</div>' +
      '</div>'
    )).join("");
  }

  function setPill(key, cls, label) { const p = $("pill-" + key); if (p) { p.className = "pill " + cls; p.textContent = label; } }
  function setCount(key, n) { const c = $("cnt-" + key); if (c) c.textContent = n ? "(" + n + ")" : ""; }

  // ---- progressive accumulators (filled item-by-item as the stream lands) ----
  let kiAcc = [], mileAcc = [], riskAcc = [];

  // ---- renderers ----
  function renderSummary(text) {
    $("body-summary").innerHTML = '<div class="summary-txt">' + esc(text || "") + "</div>";
    setPill("summary", "ok", "Ready");
  }
  function renderKeyInfo() {
    setCount("keyinfo", kiAcc.length);
    $("body-keyinfo").innerHTML = kiAcc.length
      ? kiAcc.map(k => '<div class="kv"><span class="k">' + esc(k.field_name) + '</span><span class="v">' + esc(k.value) + "</span></div>").join("")
      : '<div class="empty-note">No key fields extracted.</div>';
    setPill("keyinfo", "ok", "Ready");
  }
  function renderMilestones() {
    setCount("milestones", mileAcc.length);
    $("body-milestones").innerHTML = mileAcc.length
      ? mileAcc.map(m => (
          '<div class="mile"><div class="mt">' + esc(m.milestone_name) + "</div>" +
          '<div class="md">' + esc(m.date_or_trigger) + "</div>" +
          '<div class="mb">' + esc(m.description) + "</div></div>"
        )).join("")
      : '<div class="empty-note">No dated milestones found.</div>';
    setPill("milestones", "ok", "Ready");
  }
  function renderRisks() {
    setCount("risks", riskAcc.length);
    $("body-risks").innerHTML = riskAcc.length
      ? riskAcc.map(r => (
          '<div class="risk ' + esc(r.severity) + '">' +
            '<div class="risk-top"><span class="risk-title">' + esc(r.clause_title) + "</span>" +
            '<span class="sev ' + esc(r.severity) + '">' + esc(r.severity) + "</span></div>" +
            '<div class="risk-row"><b>Issue.</b> ' + esc(r.issue) + "</div>" +
          "</div>"
        )).join("")
      : '<div class="empty-note">No material risks identified.</div>';
    setPill("risks", "ok", "Ready");
  }

  // Append one streamed item to its card and re-render that card.
  function addItem(section, value) {
    if (section === "key_information") { kiAcc.push(value); renderKeyInfo(); }
    else if (section === "timeline_and_key_milestones") { mileAcc.push(value); renderMilestones(); }
    else if (section === "risk_and_compliance_insights") { riskAcc.push(value); renderRisks(); }
  }

  function markAllFailed() {
    ["summary", "keyinfo", "milestones", "risks"].forEach(k => { const pill = $("pill-" + k); if (pill && pill.classList.contains("wait")) setPill(k, "err", "Failed"); });
  }

  // ---- document rendering ----
  function renderDocument(text) {
    $("pageEmpty") && ($("pageEmpty").style.display = "none");
    const lines = (text || "").split("\\n").map(l => l.trim()).filter(Boolean);
    let html = "";
    lines.forEach((ln, i) => {
      const isHeadingish = ln.length <= 70 && (ln === ln.toUpperCase() || /^(\\d+\\.|\\d+\\.\\d+|ARTICLE|SECTION)\\b/i.test(ln)) && !/[.;:]$/.test(ln);
      if (i === 0 && ln.length <= 80) html += '<h1 class="doc-h">' + esc(ln) + "</h1>";
      else if (isHeadingish) html += '<div class="doc-h2">' + esc(ln) + "</div>";
      else html += "<p>" + esc(ln) + "</p>";
    });
    $("page").innerHTML = html || '<p class="ph">(empty document)</p>';
  }

  // ---- progress / timer ----
  let received = 0, tickId = null, startT = 0, firstAt = null, busy = false;
  function startTimer() { startT = performance.now(); tickId = setInterval(() => { $("elapsed").textContent = ((performance.now() - startT) / 1000).toFixed(1) + "s"; }, 100); }
  function stopTimer() { if (tickId) clearInterval(tickId); tickId = null; }
  function setStatus(html) { $("statusWrap").innerHTML = html; }
  function bumpProgress() { received++; $("pfill").style.width = Math.min(92, 8 + received * 4) + "%"; $("secCount").textContent = received + (received === 1 ? " item" : " items"); }

  function resetRun() {
    received = 0; kiAcc = []; mileAcc = []; riskAcc = []; firstAt = null;
    buildCards(); $("pfill").style.width = "0%"; $("secCount").textContent = "waiting";
    $("firstMeta").textContent = ""; $("elapsed").textContent = "0.0s";
    $("footPulse").innerHTML = '<span class="live-dot"></span> streaming';
  }
  function lockUI(on) { busy = on; $("run").disabled = on; $("runClassic").disabled = on; $("ribbonAnalyze").style.opacity = on ? .5 : 1; }

  // ---- streaming run ----
  async function runStream() {
    const f = $("file").files[0]; if (!f || busy) return;
    lockUI(true); resetRun();
    setStatus('<span class="spin"></span><span id="statusText">Connecting&hellip;</span>');
    startTimer();
    const fd = new FormData(); fd.append("file", f);
    try {
      const res = await fetch(STREAM_URL, { method: "POST", headers: { "X-Session-Id": SESSION }, body: fd });
      if (!res.ok) throw new Error("HTTP " + res.status);
      setStatus('<span class="spin"></span><span id="statusText">Analyzing&hellip;</span>');
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\\n\\n")) >= 0) { const frame = buf.slice(0, i); buf = buf.slice(i + 2); handleFrame(frame); }
      }
      finishRun(false);
    } catch (e) {
      console.error(e);
      setStatus('<span id="statusText" style="color:var(--crit)">Error: ' + esc(e.message) + "</span>");
      finishRun(true);
    }
  }

  function handleFrame(frame) {
    let ev = "message", data = "";
    frame.split("\\n").forEach(line => {
      if (line.startsWith("event:")) ev = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    });
    if (!data) return;
    let d; try { d = JSON.parse(data); } catch (_) { return; }

    function markFirst() {
      if (firstAt === null) { firstAt = performance.now(); $("firstMeta").textContent = "first in " + ((firstAt - startT) / 1000).toFixed(1) + "s"; }
    }
    if (ev === "document") { renderDocument(d.text); }
    else if (ev === "start") { setStatus('<span class="spin"></span><span id="statusText">' + (d.cached ? "Loading saved analysis&hellip;" : "Analyzing contract&hellip;") + "</span>"); }
    else if (ev === "summary") {
      markFirst();
      renderSummary(d.text);
      setStatus('<span class="spin"></span><span id="statusText">Streaming analysis&hellip;</span>');
    }
    else if (ev === "item") {
      markFirst();
      bumpProgress();
      addItem(d.section, d.value);
    }
    else if (ev === "done") { /* handled by stream end */ }
    else if (ev === "error") { markAllFailed(); setStatus('<span id="statusText" style="color:var(--crit)">Stream error</span>'); }
  }

  function finishRun(errored) {
    stopTimer();
    CARD_DEFS.forEach(c => { const pill = $("pill-" + c.key); if (pill && pill.classList.contains("wait")) { $("body-" + c.key).innerHTML = '<div class="empty-note">No content.</div>'; setPill(c.key, "ok", "Ready"); } });
    const total = ((performance.now() - startT) / 1000).toFixed(1);
    if (!errored) setStatus('<span class="live-dot" style="animation:none"></span><span id="statusText" style="color:var(--ok)">Complete &middot; ' + total + "s</span>");
    $("pfill").style.width = "100%";
    $("footPulse").innerHTML = "done in " + total + "s";
    lockUI(false);
  }

  // ---- classic (non-streaming) contrast ----
  async function runClassic() {
    const f = $("file").files[0]; if (!f || busy) return;
    lockUI(true); resetRun();
    $("footPulse").innerHTML = '<span class="spin"></span> waiting (classic)';
    setStatus('<span class="spin"></span><span id="statusText">Classic mode: waiting for the full analysis&hellip;</span>');
    startTimer();
    const fd = new FormData(); fd.append("file", f);
    try {
      const res = await fetch(CLASSIC_URL, { method: "POST", headers: { "X-Session-Id": SESSION + "-classic" }, body: fd });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      renderSummary(d.summary);
      kiAcc = d.key_information || []; renderKeyInfo();
      mileAcc = d.timeline_and_key_milestones || []; renderMilestones();
      riskAcc = d.risk_and_compliance_insights || []; renderRisks();
      received = kiAcc.length + mileAcc.length + riskAcc.length; $("pfill").style.width = "100%"; $("secCount").textContent = "analysis ready";
      const total = ((performance.now() - startT) / 1000).toFixed(1);
      $("firstMeta").textContent = "first in " + total + "s";
      setStatus('<span id="statusText" style="color:var(--ink-dim)">Classic done &middot; ' + total + "s (nothing shown until now)</span>");
      $("footPulse").innerHTML = "classic: " + total + "s";
    } catch (e) {
      setStatus('<span id="statusText" style="color:var(--crit)">Error: ' + esc(e.message) + "</span>");
    } finally { stopTimer(); lockUI(false); }
  }

  // ---- wiring ----
  $("fileBtn").addEventListener("click", () => $("file").click());
  $("file").addEventListener("change", () => {
    const f = $("file").files[0];
    $("fileName").textContent = f ? f.name : "No file selected";
    $("run").disabled = !f; $("runClassic").disabled = !f;
  });
  $("run").addEventListener("click", runStream);
  $("ribbonAnalyze").addEventListener("click", () => { if (!$("run").disabled) runStream(); else $("file").click(); });
  $("runClassic").addEventListener("click", runClassic);

  // Drag & drop onto the page
  const canvas = $("canvas");
  canvas.addEventListener("dragover", e => { e.preventDefault(); });
  canvas.addEventListener("drop", e => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && f.name.toLowerCase().endsWith(".docx")) {
      const dt = new DataTransfer(); dt.items.add(f); $("file").files = dt.files;
      $("file").dispatchEvent(new Event("change"));
    }
  });

  buildCards();
</script>
</body>
</html>"""

    @app.get("/demo/contract-analyzer", response_class=HTMLResponse)
    async def demo_contract_analyzer_ui() -> HTMLResponse:
        """Word add-in mockup that streams the real /contract-analyzer/stream endpoint."""

        return HTMLResponse(content=_DEMO_CONTRACT_ANALYZER_HTML)


def main_entry() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main_entry()
