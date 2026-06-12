from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .config import Settings
from .prompts import (
    SALES_COACH_CHAT_SYSTEM_PROMPT,
    SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT,
    SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
    SALES_COACH_STRUCTURED_SYSTEM_PROMPT,
    SALES_COACH_SYSTEM_PROMPT,
)
from .providers import (
    CerebrasClient,
    ProviderError,
    VertexClient,
    cerebras_structured_response_format,
    parse_bos_eos_text,
    parse_json_suggestion,
    strip_outer_quotes,
)
from .schemas import ChatRequest, HelpRequest, LiveRequest, LiveResponse, OpenerResponse


FALLBACK_HELP_OPENER_TEXT = (
    "Слышу, что сейчас много сомнений и риска. Давайте спокойно разберем, "
    "что именно не сработало раньше и что должно быть иначе, чтобы вам было "
    "безопасно двигаться дальше."
)


@dataclass
class OpenerAttempt:
    slot: str
    model: str | None
    text: str | None
    error: str | None = None
    rate_limited: bool = False
    timeout: bool = False

    @property
    def success(self) -> bool:
        return bool(self.text and self.text.strip())


class LlmOrchestrator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.timeout_secs,
            proxy=settings.outbound_proxy,
        )
        self._owns_client = client is None
        self.cerebras = CerebrasClient(settings, self.client)
        self.vertex = VertexClient(settings, self.client)
        self._opener_cooldowns: dict[str, float] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def status(self) -> str:
        return "ready" if self._any_provider_configured() else "disabled"

    async def live(self, request: LiveRequest) -> LiveResponse:
        if self._prefer_vertex():
            suggestion = await self._vertex_live(request)
            return LiveResponse(
                action=suggestion["action"],
                text=suggestion["text"],
                provider="vertex",
                model=self.settings.vertex_model,
            )

        if not self.cerebras.configured():
            if self.vertex.configured():
                suggestion = await self._vertex_live(request)
                return LiveResponse(
                    action=suggestion["action"],
                    text=suggestion["text"],
                    provider="vertex",
                    model=self.settings.vertex_model,
                )
            raise ProviderError("service", "no LLM provider configured")

        try:
            suggestion = await self._cerebras_live_structured(request)
        except ProviderError as exc:
            if exc.is_structured_output_error:
                suggestion = await self._cerebras_live_unstructured(request)
            elif exc.is_rate_limit and self._auto_provider() and self.vertex.configured():
                suggestion = await self._vertex_live(request)
                return LiveResponse(
                    action=suggestion["action"],
                    text=suggestion["text"],
                    provider="vertex",
                    model=self.settings.vertex_model,
                )
            else:
                raise
        except (ValueError, json.JSONDecodeError):
            suggestion = await self._cerebras_live_unstructured(request)

        return LiveResponse(
            action=suggestion["action"],
            text=suggestion["text"],
            provider="cerebras",
            model=self.settings.cerebras_model,
        )

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[bytes]:
        user_content = f"{request.context}\n\n--- Вопрос продавца ---\n{request.question}\n"
        if self._prefer_vertex() or not self.cerebras.configured():
            async for event in self._vertex_text_stream(
                system_prompt=SALES_COACH_CHAT_SYSTEM_PROMPT,
                user_content=user_content,
                temperature=0.35,
            ):
                yield event
            return

        try:
            yield sse_event({"event": "model", "model": self.settings.cerebras_model})
            async for delta in self.cerebras.stream_text(
                model=self.settings.cerebras_model,
                system_prompt=SALES_COACH_CHAT_SYSTEM_PROMPT,
                user_content=user_content,
                temperature=0.35,
                prompt_cache_key=f"rec-sidecar-sales-chat-v1-{request.run_id}",
            ):
                yield sse_event({"event": "delta", "text": delta})
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            if exc.is_rate_limit and self._auto_provider() and self.vertex.configured():
                async for event in self._vertex_text_stream(
                    system_prompt=SALES_COACH_CHAT_SYSTEM_PROMPT,
                    user_content=user_content,
                    temperature=0.35,
                ):
                    yield event
            else:
                yield sse_event({"event": "error", "message": str(exc)})

    async def help_opener(self, request: HelpRequest) -> OpenerResponse:
        if not self.cerebras.configured():
            return OpenerResponse(text=FALLBACK_HELP_OPENER_TEXT, fallback=True)

        primary_model = self._ready_opener_model(
            "primary", self.settings.help_opener_primary_model
        )
        secondary_model = self._ready_opener_model(
            "secondary", self.settings.help_opener_secondary_model
        )
        primary, secondary = await asyncio.gather(
            self._opener_attempt("primary", primary_model, request),
            self._opener_attempt("secondary", secondary_model, request),
        )
        self._apply_opener_attempt(primary)
        self._apply_opener_attempt(secondary)

        for attempt in (primary, secondary):
            if attempt.success:
                return OpenerResponse(
                    text=attempt.text or "",
                    model=attempt.model,
                    fallback=False,
                )

        return OpenerResponse(text=FALLBACK_HELP_OPENER_TEXT, fallback=True)

    async def help_constructive_stream(self, request: HelpRequest) -> AsyncIterator[bytes]:
        if not self.vertex.configured():
            yield sse_event({"event": "done"})
            return

        user_content = (
            f"{request.context}\n\n--- Задача ---\n"
            "Подготовь один короткий следующий ход продавцу для текущего момента звонка."
        )
        async for event in self._vertex_text_stream(
            system_prompt=SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT,
            user_content=user_content,
            temperature=0.35,
        ):
            yield event

    async def _cerebras_live_structured(self, request: LiveRequest) -> dict[str, str]:
        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_STRUCTURED_SYSTEM_PROMPT,
            user_content=request.content,
            temperature=0.2,
            prompt_cache_key=f"rec-sidecar-sales-coach-v2-{request.run_id}",
            response_format=cerebras_structured_response_format(),
        )
        return parse_json_suggestion(text)

    async def _cerebras_live_unstructured(self, request: LiveRequest) -> dict[str, str]:
        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_SYSTEM_PROMPT,
            user_content=request.content,
            temperature=0.25,
            prompt_cache_key=f"rec-sidecar-sales-coach-v1-{request.run_id}",
        )
        return parse_bos_eos_text(text)

    async def _vertex_live(self, request: LiveRequest) -> dict[str, str]:
        if not self.vertex.configured():
            raise ProviderError("vertex", "Vertex is not configured")
        return await self.vertex.generate_structured(
            system_prompt=SALES_COACH_STRUCTURED_SYSTEM_PROMPT,
            user_content=request.content,
            temperature=0.2,
        )

    async def _vertex_text_stream(
        self,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> AsyncIterator[bytes]:
        try:
            yield sse_event({"event": "model", "model": self.settings.vertex_model})
            async for delta in self.vertex.stream_text(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            ):
                yield sse_event({"event": "delta", "text": delta})
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            yield sse_event({"event": "error", "message": str(exc)})

    def _ready_opener_model(self, slot: str, model: str) -> str | None:
        blocked_until = self._opener_cooldowns.get(slot)
        if blocked_until and blocked_until > time.monotonic():
            return None
        self._opener_cooldowns.pop(slot, None)
        return model

    async def _opener_attempt(
        self, slot: str, model: str | None, request: HelpRequest
    ) -> OpenerAttempt:
        if not model:
            return OpenerAttempt(slot=slot, model=None, text=None)
        user_content = (
            f"{request.context}\n\n--- Задача ---\n"
            "Дай одну короткую эмпатичную фразу-мостик, которую продавец может сразу "
            "прочитать клиенту вслух."
        )
        try:
            text = await asyncio.wait_for(
                self.cerebras.text(
                    model=model,
                    system_prompt=SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
                    user_content=user_content,
                    temperature=0.25,
                    prompt_cache_key=f"rec-sidecar-help-opener-v1-{request.run_id}",
                ),
                timeout=self.settings.help_opener_timeout_ms / 1000.0,
            )
            text = strip_outer_quotes(text)
            return OpenerAttempt(slot=slot, model=model, text=text or None)
        except asyncio.TimeoutError:
            return OpenerAttempt(slot=slot, model=model, text=None, timeout=True)
        except ProviderError as exc:
            return OpenerAttempt(
                slot=slot,
                model=model,
                text=None,
                error=str(exc),
                rate_limited=exc.is_rate_limit,
            )

    def _apply_opener_attempt(self, attempt: OpenerAttempt) -> None:
        if attempt.rate_limited:
            self._opener_cooldowns[attempt.slot] = (
                time.monotonic() + self.settings.rate_limit_backoff_ms / 1000.0
            )
        elif attempt.success:
            self._opener_cooldowns.pop(attempt.slot, None)

    def _prefer_vertex(self) -> bool:
        return self.settings.provider in {"vertex", "gemini", "google"}

    def _auto_provider(self) -> bool:
        return self.settings.provider not in {"cerebras", "vertex", "gemini", "google"}

    def _any_provider_configured(self) -> bool:
        if self._prefer_vertex():
            return self.vertex.configured()
        if self.settings.provider == "cerebras":
            return self.cerebras.configured()
        return self.cerebras.configured() or self.vertex.configured()


def sse_event(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {text}\n\n".encode("utf-8")
