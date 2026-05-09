import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

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
from src.api.endpoints.doc_information.router import router as doc_information_router
from src.api.endpoints.ingestion.router import router as ingestion_router
from src.api.endpoints.orchestrator.router import router as orchestrator_router
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
app.include_router(admin_router, prefix="/admin")
app.include_router(orchestrator_router, prefix="/api/v1/orchesrator")
app.include_router(doc_information_router, prefix="/api/v1/DocInfo")
app.include_router(agents_router, prefix="/Accorder/agents")


def main_entry() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main_entry()
