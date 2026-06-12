from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    status: str
    provider: str
    model: str
    cerebras_configured: bool
    vertex_configured: bool


class LiveRequest(StrictModel):
    run_id: str
    content: str
    force: bool = False


class LiveResponse(BaseModel):
    action: Literal["skip", "suggest"]
    text: str = ""
    provider: str
    model: str


class ChatRequest(StrictModel):
    id: int
    run_id: str
    context: str
    question: str


class HelpRequest(StrictModel):
    id: int
    run_id: str
    context: str


class StageRequest(StrictModel):
    run_id: str
    context: str
    current_stage: str | None = None


class StageScoreEvidence(BaseModel):
    speaker: str | None = None
    quote: str


class StageScoreCheck(BaseModel):
    id: str
    label: str
    level: Literal["core", "quality", "hygiene"]
    result: Literal["hit", "miss", "pending", "uncertain", "na"]
    signal: Literal[
        "balance",
        "dialogue",
        "pain",
        "specificity",
        "trust",
        "focus",
        "transition",
    ]
    reason: str
    evidence: list[StageScoreEvidence] = []


class StageScoreSignal(BaseModel):
    id: Literal["balance", "dialogue", "pain", "specificity", "trust", "focus"]
    label: str
    state: Literal["green", "yellow", "red", "gray"]
    detail: str


class StageScorecard(BaseModel):
    readiness: Literal["green", "yellow", "red", "pending"]
    readiness_label: str
    score: float | None = None
    hit_count: int
    miss_count: int
    total_count: int
    hard_red: bool = False
    ready_to_advance: bool = False
    next_action: str
    summary: str
    checks: list[StageScoreCheck] = []
    signals: list[StageScoreSignal] = []


class StageAgendaResponse(BaseModel):
    stage: str
    title: str
    agenda: str
    emotion: str
    step: str
    provider: str
    model: str
    confidence: float | None = None
    scorecard: StageScorecard | None = None


class OpenerResponse(BaseModel):
    text: str
    model: str | None = None
    fallback: bool = False
