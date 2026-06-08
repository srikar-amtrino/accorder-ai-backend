import time
import typing
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# from src.api.endpoints.admin.router import router as admin_router
from src.api.endpoints.agents.main import router as agents_router

# from src.api.endpoints.clause_extraction.router import (
#     router as clause_extraction_router,
# )
# from src.api.endpoints.describe_draft.router import router as describe_draft_router
from src.api.endpoints.ingestion.router import router as ingestion_router
from src.config.logging import setup_logging
from src.config.settings import get_settings
from src.core.container import container

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> typing.AsyncGenerator[Any, Any]:
    # Startup
    container.initialize()
    yield
    # Shutdown


app = FastAPI(
    title="Contract Review API",
    version="1.0.0",
    debug=settings.debug,
    lifespan=lifespan,
)


_cors_origins = settings.cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time"],
)


# Add request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next: typing.Callable) -> Request:
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


app.include_router(ingestion_router, prefix="/ingestion")
# app.include_router(admin_router, prefix="/admin")
app.include_router(agents_router, prefix="/Accorder/agents")


def main_entry() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main_entry()
