from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

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
)
from .schemas import ChatRequest, HelpRequest, LiveRequest, LiveResponse, OpenerResponse


logger = logging.getLogger("uvicorn.error")
HELP_TEMPERATURE = 1.0

FALLBACK_HELP_OPENER_TEXT = (
    "Давайте зафиксируем главный риск и разберем, что должно быть иначе, "
    "чтобы вам было безопасно двигаться дальше."
)


@dataclass
class OpenerCandidate:
    slot: str
    priority: int
    provider: str
    model: str | None
    stream: AsyncIterator[str] | None


@dataclass
class OpenerFirstDelta:
    candidate: OpenerCandidate
    delta: str | None = None
    error: Exception | None = None


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
        text_parts: list[str] = []
        model: str | None = None
        fallback = False

        async for frame in self.help_opener_stream(request):
            data = frame.decode("utf-8").removeprefix("data:").strip()
            if not data:
                continue
            event = json.loads(data)
            match event.get("event"):
                case "model":
                    model = event.get("model")
                case "delta":
                    text_parts.append(event.get("text", ""))
                case "fallback":
                    fallback = True
                case "error":
                    raise ProviderError("service", event.get("message", "opener stream error"))

        return OpenerResponse(
            text="".join(text_parts).strip() or FALLBACK_HELP_OPENER_TEXT,
            model=model,
            fallback=fallback,
        )

    async def help_opener_stream(self, request: HelpRequest) -> AsyncIterator[bytes]:
        candidates = self._opener_candidates(request)
        if not candidates:
            logger.info(
                "help_opener fallback id=%s run_id=%s reason=no_candidates",
                request.id,
                request.run_id,
            )
            async for event in self._fallback_opener_stream():
                yield event
            return

        started_at = time.monotonic()
        timeout = self.settings.help_opener_timeout_ms / 1000.0
        pending: dict[asyncio.Task[OpenerFirstDelta], OpenerCandidate] = {
            asyncio.create_task(self._first_opener_delta(candidate)): candidate
            for candidate in candidates
            if candidate.stream is not None
        }
        deadline = time.monotonic() + timeout

        first_results: dict[str, OpenerFirstDelta] = {}
        winner: OpenerFirstDelta | None = None

        try:
            while pending:
                winner = best_ready_opener_result(first_results, pending.values())
                if winner:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait(
                    pending.keys(),
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break

                for task in done:
                    candidate = pending.pop(task)
                    result = await task
                    first_results[candidate.slot] = result
                    elapsed_ms = int((time.monotonic() - started_at) * 1000)
                    if result.delta:
                        logger.info(
                            "help_opener first_delta id=%s run_id=%s slot=%s provider=%s model=%s priority=%s elapsed_ms=%s chars=%s",
                            request.id,
                            request.run_id,
                            candidate.slot,
                            candidate.provider,
                            candidate.model,
                            candidate.priority,
                            elapsed_ms,
                            len(result.delta),
                        )
                    elif result.error:
                        logger.info(
                            "help_opener candidate_error id=%s run_id=%s slot=%s provider=%s model=%s elapsed_ms=%s error=%s",
                            request.id,
                            request.run_id,
                            candidate.slot,
                            candidate.provider,
                            candidate.model,
                            elapsed_ms,
                            result.error,
                        )
                    else:
                        logger.info(
                            "help_opener candidate_empty id=%s run_id=%s slot=%s provider=%s model=%s elapsed_ms=%s",
                            request.id,
                            request.run_id,
                            candidate.slot,
                            candidate.provider,
                            candidate.model,
                            elapsed_ms,
                        )
                    if isinstance(result.error, ProviderError) and result.error.is_rate_limit:
                        self._opener_cooldowns[candidate.slot] = (
                            time.monotonic() + self.settings.rate_limit_backoff_ms / 1000.0
                        )

            if winner is None:
                winner = best_ready_opener_result(
                    first_results, pending.values()
                ) or best_opener_result(first_results.values())

            if winner and winner.delta:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                await self._cancel_opener_candidates(pending)
                await close_opener_results(first_results.values(), keep=winner.candidate)
                logger.info(
                    "help_opener selected id=%s run_id=%s slot=%s provider=%s model=%s priority=%s elapsed_ms=%s ready=%s",
                    request.id,
                    request.run_id,
                    winner.candidate.slot,
                    winner.candidate.provider,
                    winner.candidate.model,
                    winner.candidate.priority,
                    elapsed_ms,
                    ",".join(
                        f"{result.candidate.slot}:{result.candidate.model}"
                        for result in sorted(
                            first_results.values(),
                            key=lambda result: result.candidate.priority,
                        )
                        if result.delta
                    ),
                )
                yield sse_event(
                    {
                        "event": "model",
                        "model": winner.candidate.model,
                        "provider": winner.candidate.provider,
                    }
                )
                first_delta = normalize_opener_delta(winner.delta, first=True)
                if first_delta:
                    yield sse_event({"event": "delta", "text": first_delta})
                if winner.candidate.stream is not None:
                    async for delta in winner.candidate.stream:
                        delta = normalize_opener_delta(delta)
                        if delta:
                            yield sse_event({"event": "delta", "text": delta})
                yield sse_event({"event": "done"})
                return
        finally:
            await self._cancel_opener_candidates(pending)
            await close_opener_results(
                first_results.values(),
                keep=winner.candidate if winner else None,
            )

        logger.info(
            "help_opener fallback id=%s run_id=%s reason=no_winner elapsed_ms=%s",
            request.id,
            request.run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        async for event in self._fallback_opener_stream():
            yield event

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
            temperature=HELP_TEMPERATURE,
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

    def _opener_candidates(self, request: HelpRequest) -> list[OpenerCandidate]:
        user_content = (
            f"{request.context}\n\n--- Задача ---\n"
            "Дай одну короткую эмпатичную фразу-мостик, которую продавец может сразу "
            "прочитать клиенту вслух."
        )
        candidates: list[OpenerCandidate] = []

        use_cerebras = self.settings.provider == "cerebras" or self._auto_provider()
        use_vertex = self._prefer_vertex() or self._auto_provider()

        vertex_model = self._ready_opener_model("vertex", self.settings.vertex_model)
        if use_vertex and self.vertex.configured() and vertex_model:
            candidates.append(
                OpenerCandidate(
                    slot="vertex",
                    priority=0,
                    provider="vertex",
                    model=vertex_model,
                    stream=self.vertex.stream_text(
                        system_prompt=SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
                        user_content=user_content,
                        temperature=HELP_TEMPERATURE,
                        thinking_level=self.settings.vertex_thinking_level,
                    ),
                )
            )

        if use_cerebras and self.cerebras.configured():
            for slot, model in (
                (
                    "primary",
                    self._ready_opener_model(
                        "primary", self.settings.help_opener_primary_model
                    ),
                ),
                (
                    "secondary",
                    self._ready_opener_model(
                        "secondary", self.settings.help_opener_secondary_model
                    ),
                ),
            ):
                if not model:
                    continue
                candidates.append(
                    OpenerCandidate(
                        slot=slot,
                        priority=1 if slot == "primary" else 2,
                        provider="cerebras",
                        model=model,
                        stream=self.cerebras.stream_text(
                            model=model,
                            system_prompt=SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
                            user_content=user_content,
                            temperature=HELP_TEMPERATURE,
                            prompt_cache_key=f"rec-sidecar-help-opener-v1-{request.run_id}",
                        ),
                    )
                )

        return candidates

    async def _first_opener_delta(self, candidate: OpenerCandidate) -> OpenerFirstDelta:
        if candidate.stream is None:
            return OpenerFirstDelta(candidate=candidate)
        try:
            async for delta in candidate.stream:
                if delta and delta.strip():
                    return OpenerFirstDelta(candidate=candidate, delta=delta)
            return OpenerFirstDelta(candidate=candidate)
        except ProviderError as exc:
            return OpenerFirstDelta(candidate=candidate, error=exc)
        except Exception as exc:
            return OpenerFirstDelta(candidate=candidate, error=exc)

    async def _cancel_opener_candidates(
        self, pending: dict[asyncio.Task[OpenerFirstDelta], OpenerCandidate]
    ) -> None:
        candidates = list(pending.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)
        for candidate in candidates:
            await close_async_iterator(candidate.stream)
        pending.clear()

    async def _fallback_opener_stream(self) -> AsyncIterator[bytes]:
        yield sse_event({"event": "fallback"})
        yield sse_event({"event": "delta", "text": FALLBACK_HELP_OPENER_TEXT})
        yield sse_event({"event": "done"})

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


def normalize_opener_delta(delta: str, first: bool = False) -> str:
    if first:
        return delta.lstrip().lstrip("\"'«»“”")
    return delta


def best_ready_opener_result(
    results: dict[str, OpenerFirstDelta],
    pending_candidates: Iterable[OpenerCandidate],
) -> OpenerFirstDelta | None:
    best = best_opener_result(results.values())
    if best is None:
        return None
    best_priority = best.candidate.priority
    if any(candidate.priority < best_priority for candidate in pending_candidates):
        return None
    return best


def best_opener_result(results: Iterable[OpenerFirstDelta]) -> OpenerFirstDelta | None:
    successes = [result for result in results if result.delta]
    if not successes:
        return None
    return min(successes, key=lambda result: result.candidate.priority)


async def close_opener_results(
    results: Iterable[OpenerFirstDelta], keep: OpenerCandidate | None = None
) -> None:
    for result in results:
        if keep is not None and result.candidate is keep:
            continue
        await close_async_iterator(result.candidate.stream)


async def close_async_iterator(stream: AsyncIterator[str] | None) -> None:
    if stream is None:
        return
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        with suppress(Exception):
            await aclose()
