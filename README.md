# Accorder AI — Contract Review Backend

> A production-grade FastAPI backend for AI-assisted contract review, powered by **Claude Opus 4.7** on **AWS Bedrock**.

Accorder AI ingests legal contracts, performs semantic chunking and embedding, and exposes a suite of LLM-powered endpoints for summarization, structured analysis, clause comparison, playbook validation, document Q&A, and AI-assisted drafting. Built for legal and procurement teams who need fast, accurate, and explainable contract review at scale.

---

## Capabilities

- **End-to-end contract analysis** — summary, key information, timeline of milestones, and risk and compliance insights returned in a single structured response.
- **Semantic document Q&A** — RAG-backed natural-language queries against any ingested contract.
- **Clause-level review** — apply/dismiss suggestions grounded in the actual contract text, anchored to verbatim substrings.
- **Playbook validation** — evaluate a document against a custom rule set, including detection of missing clauses.
- **Document comparison** — clause-by-clause diff between two contract versions, classified by change type, modification type, and risk level.
- **AI-assisted drafting** — generate enforceable clauses or complete NDAs from natural-language briefs.
- **Per-session isolation** — FAISS indices and chunk stores are scoped per session, with automatic TTL-based cleanup.

---

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/ingest/` | Ingest a DOCX into a session. |
| `POST` | `/api/v1/clause-extraction/extract-clauses/` | Pure-Python clause extraction (no LLM). |
| `GET`  | `/api/v1/DocInfo/summarizer` | Markdown summary of the ingested document. |
| `GET`  | `/api/v1/DocInfo/key-information` | Markdown key-information report. |
| `POST` | `/Accorder/agents/contract-analyzer` | Structured JSON analysis — summary, key info, timeline, risks. |
| `POST` | `/Accorder/agents/query-document` | Document Q&A (query rewrite → retrieve → answer). |
| `POST` | `/Accorder/agents/general-review` | Clause-level apply/dismiss suggestions. |
| `POST` | `/Accorder/agents/playbook-review` | Rule-based document validation. |
| `POST` | `/Accorder/agents/compare-documents` | Multi-stage diff between two DOCX versions. |
| `POST` | `/Accorder/agents/draft` | Draft a clause or full NDA from a natural-language brief. |
| `GET`  | `/admin/sessions/` *(and friends)* | Session admin — list, inspect, delete, manual cleanup. |

Every endpoint accepts an `X-Session-ID` header. The same identifier ties multi-step workflows together.

Interactive API documentation is available at `http://<host>:8000/docs` once the server is running.

---

## Tech Stack

- **API:** FastAPI · Uvicorn · Pydantic v2
- **LLM:** Claude Opus 4.7 via AWS Bedrock (streaming `invoke_model_with_response_stream` with Anthropic tool-use forcing for structured outputs)
- **Embeddings:** sentence-transformers / MiniLM-L6-v2, running locally on CPU
- **Vector store:** FAISS, in-memory and per-session
- **Document parsing:** python-docx · LangChain `RecursiveCharacterTextSplitter`
- **Prompt templating:** Mustache, versioned under `prompts/v1/`

---

## Quick Start

### Local Development

```bash
git clone <repo-url> && cd accorder-ai-backend

poetry env use python3.10
poetry lock && poetry install

cp .env.example .env
# fill in BEDROCK_MODEL_ID and AWS credentials (see Configuration)

poetry run python -m src.api.main
```

Server binds to `http://localhost:8000` by default. Swagger UI at `/docs`.

### EC2 Deployment

The recommended target is an EC2 instance with an IAM role that grants `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the configured Bedrock model or inference profile. The instance metadata service supplies credentials automatically — no AWS keys in `.env`.

```bash
git clone <repo-url> && cd accorder-ai-backend

poetry env use python3.10
poetry lock && poetry install

cat > .env <<EOF
API_HOST=0.0.0.0
AWS_REGION=us-west-1
BEDROCK_MODEL_ID=<your inference profile ARN>
EOF

poetry run python -m src.api.main
```

For browser access from outside the instance, add an inbound rule for TCP 8000 to the EC2 security group, scoped to authorized IPs.

---

## Configuration

All settings are read from `.env` via `pydantic-settings`.

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Interface to bind. `0.0.0.0` listens on all interfaces; `localhost` for local-only. |
| `API_PORT` | `8000` | Port. |
| `DEBUG` | `false` | Verbose logging. |
| `CHUNK_SIZE` | `1000` | Maximum chunk size during DOCX parsing. |
| `CHUNK_OVERLAP` | `200` | Overlap between chunks. |
| `AWS_REGION` | `us-west-1` | Bedrock region. |
| `BEDROCK_MODEL_ID` | *(required)* | Bedrock model id or inference-profile ARN for Anthropic Claude. |
| `SESSION_TTL_MINUTES` | `120` | Session expiry — defaults to 2 hours. |
| `SESSION_CLEANUP_INTERVAL_MINUTES` | `10.0` | How often the background worker checks for expired sessions. |

---

## Testing

Two smoke-test scripts ship with the repository.

```bash
# 1. Bedrock connectivity — verifies credentials, region, model id, and IAM permissions
poetry run python scripts/test_bedrock.py

# 2. End-to-end endpoint sweep — boots through every LLM-using endpoint with sample DOCX input
poetry run python scripts/create_test_docs.py
poetry run python -m src.api.main &
poetry run python scripts/test_endpoints.py
```

The endpoint sweep reports pass/fail per endpoint and dumps the server response on failure for fast debugging.

---

## Design Principles

- One conceptual unit per file.
- Prompts are versioned Mustache templates — never inline strings.
- Every exception is a named subclass of `AppException`. No bare `Exception` raises.
- All inputs and outputs are explicitly typed and validated through Pydantic.
- LLM calls flow through the abstract `BaseLLMModel.generate(...)` interface — swapping providers is a one-line container change.
- Per-session state lives in `SessionManager`. No shared mutable globals for user data.

---

## Operational Notes

- **Logging.** Application logs are rotated daily to `logs/AI_Contract_Review_YYYYMMDD.log`. ERROR-level events are mirrored to `logs/errors.log`.
- **Bedrock quotas.** Claude Opus has a per-account TPS quota — typically 1–4 requests per second by default. Fan-out endpoints (`compare-documents`, `general-review`, `playbook-review`) may encounter `ThrottlingException` under heavy load. Request a quota increase via AWS Service Quotas as traffic grows.
- **Prompt versioning.** Prompt templates live under `src/services/prompts/v1/`. New revisions ship under `v2/` and are activated by changing the caller's template path — no schema or interface changes required.
