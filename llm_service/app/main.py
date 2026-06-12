from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from .config import Settings
from .orchestrator import LlmOrchestrator
from .providers import ProviderError
from .schemas import (
    ChatRequest,
    HealthResponse,
    HelpRequest,
    LiveRequest,
    LiveResponse,
    OpenerResponse,
)


settings = Settings.from_env()
orchestrator = LlmOrchestrator(settings)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await orchestrator.aclose()


app = FastAPI(title="rec-sidecar LLM service", version="0.1.0", lifespan=lifespan)


async def require_service_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.service_token:
        return
    expected = f"Bearer {settings.service_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid service token")


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(
        status=orchestrator.status(),
        provider=settings.provider_label(),
        model=settings.active_model_label(),
        cerebras_configured=settings.cerebras_configured,
        vertex_configured=settings.vertex_configured,
    )


@app.post(
    "/v1/coach/live",
    response_model=LiveResponse,
    dependencies=[Depends(require_service_token)],
)
async def live(request: LiveRequest) -> LiveResponse:
    try:
        return await orchestrator.live(request)
    except ProviderError as exc:
        raise HTTPException(status_code=provider_status(exc), detail=str(exc)) from exc


@app.post("/v1/coach/chat/stream", dependencies=[Depends(require_service_token)])
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        orchestrator.chat_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post(
    "/v1/coach/help/opener",
    response_model=OpenerResponse,
    dependencies=[Depends(require_service_token)],
)
async def help_opener(request: HelpRequest) -> OpenerResponse:
    try:
        return await orchestrator.help_opener(request)
    except ProviderError as exc:
        raise HTTPException(status_code=provider_status(exc), detail=str(exc)) from exc


@app.post("/v1/coach/help/opener/stream", dependencies=[Depends(require_service_token)])
async def help_opener_stream(request: HelpRequest) -> StreamingResponse:
    return StreamingResponse(
        orchestrator.help_opener_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/v1/coach/help/constructive/stream", dependencies=[Depends(require_service_token)])
async def help_constructive_stream(request: HelpRequest) -> StreamingResponse:
    return StreamingResponse(
        orchestrator.help_constructive_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def provider_status(exc: ProviderError) -> int:
    if exc.status_code == 429:
        return 429
    if exc.status_code in {401, 403}:
        return exc.status_code
    if "no LLM provider configured" in str(exc):
        return 503
    return 502
