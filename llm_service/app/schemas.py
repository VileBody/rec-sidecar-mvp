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


class OpenerResponse(BaseModel):
    text: str
    model: str | None = None
    fallback: bool = False
