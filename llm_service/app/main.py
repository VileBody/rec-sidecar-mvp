from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Response, WebSocket, WebSocketException, status
from fastapi.responses import StreamingResponse

from .config import Settings
from .live_asr import VertexLiveAsrBridge
from .live_stage_audio import VertexLiveStageAudioBridge
from .orchestrator import LlmOrchestrator
from .providers import ProviderError
from .schemas import (
    ChatRequest,
    HealthResponse,
    HelpRequest,
    LiveRequest,
    LiveResponse,
    OpenerResponse,
    PivotGateRequest,
    PivotGateResponse,
    ReadyGateRequest,
    ReadyGateResponse,
    StageAgendaResponse,
    StageRequest,
    StudentAnswerRequest,
    StudentTranslateRequest,
    StudentTranslateResponse,
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
        intelligence_transport=settings.intelligence_transport,
        vertex_live_model=settings.vertex_live_model,
        vertex_live_asr_model=settings.vertex_live_asr_model,
        vertex_live_asr_location=settings.vertex_live_asr_location,
        vertex_live_stage_model=settings.vertex_live_stage_model,
        vertex_live_stage_location=settings.vertex_live_stage_location,
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


@app.post(
    "/v1/coach/live/ready-gate",
    response_model=ReadyGateResponse,
    dependencies=[Depends(require_service_token)],
)
async def ready_gate(request: ReadyGateRequest) -> ReadyGateResponse:
    try:
        return await orchestrator.ready_gate(request)
    except (ProviderError, ValueError) as exc:
        status_code = provider_status(exc) if isinstance(exc, ProviderError) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post(
    "/v1/coach/live/pivot-gate",
    response_model=PivotGateResponse,
    dependencies=[Depends(require_service_token)],
)
async def pivot_gate(request: PivotGateRequest) -> PivotGateResponse:
    try:
        return await orchestrator.pivot_gate(request)
    except (ProviderError, ValueError) as exc:
        status_code = provider_status(exc) if isinstance(exc, ProviderError) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/v1/coach/chat/stream", dependencies=[Depends(require_service_token)])
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        orchestrator.chat_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post(
    "/v1/coach/stage",
    response_model=StageAgendaResponse,
    dependencies=[Depends(require_service_token)],
)
async def stage_agenda(request: StageRequest) -> StageAgendaResponse | Response:
    try:
        response = await orchestrator.stage_agenda(request)
        if response is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return response
    except (ProviderError, ValueError) as exc:
        status_code = provider_status(exc) if isinstance(exc, ProviderError) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


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


@app.post(
    "/v1/student/translate",
    response_model=StudentTranslateResponse,
    dependencies=[Depends(require_service_token)],
)
async def student_translate(request: StudentTranslateRequest) -> StudentTranslateResponse:
    try:
        return await orchestrator.student_translate(request)
    except ProviderError as exc:
        raise HTTPException(status_code=provider_status(exc), detail=str(exc)) from exc


@app.post("/v1/student/answer/stream", dependencies=[Depends(require_service_token)])
async def student_answer_stream(request: StudentAnswerRequest) -> StreamingResponse:
    return StreamingResponse(
        orchestrator.student_answer_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.websocket("/v1/asr/gemini-live")
async def gemini_live_asr(websocket: WebSocket) -> None:
    require_service_token_websocket(websocket)
    await websocket.accept()
    await VertexLiveAsrBridge(settings=settings, vertex=orchestrator.vertex).run(websocket)


@app.websocket("/v1/coach/stage/live-audio")
async def coach_stage_live_audio(websocket: WebSocket) -> None:
    require_service_token_websocket(websocket)
    await websocket.accept()
    await VertexLiveStageAudioBridge(settings=settings, vertex=orchestrator.vertex).run(websocket)


def require_service_token_websocket(websocket: WebSocket) -> None:
    if not settings.service_token:
        return
    expected = f"Bearer {settings.service_token}"
    if websocket.headers.get("authorization") != expected:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)


def provider_status(exc: ProviderError) -> int:
    if exc.status_code == 429:
        return 429
    if exc.status_code in {401, 403}:
        return exc.status_code
    if "no LLM provider configured" in str(exc):
        return 503
    return 502
