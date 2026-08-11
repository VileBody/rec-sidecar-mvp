from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import httpx

from .config import Settings
from .live_intelligence import LiveIntelligenceNoUpdate, VertexLiveIntelligenceSession
from .interview_prompts import (
    INTERVIEW_HELP_ANSWER_SYSTEM_PROMPT,
    INTERVIEW_LIVE_ANSWER_SYSTEM_PROMPT,
    INTERVIEW_QUESTION_DETECTOR_SYSTEM_PROMPT,
)
from .prompts import (
    SALES_COACH_CHAT_SYSTEM_PROMPT,
    SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT,
    SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
    SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT,
    SALES_COACH_PIVOT_GATE_SYSTEM_PROMPT,
    SALES_COACH_READY_GATE_SYSTEM_PROMPT,
    SALES_COACH_LIVE_VALIDATOR_SYSTEM_PROMPT,
    SALES_COACH_STRUCTURED_SYSTEM_PROMPT,
    SALES_COACH_SYSTEM_PROMPT,
    STUDENT_ANSWER_SYSTEM_PROMPT,
    STUDENT_HELP_SYSTEM_PROMPT,
    STUDENT_TRANSLATION_SYSTEM_PROMPT,
)
from .providers import (
    CerebrasClient,
    OpenRouterClient,
    ProviderError,
    VertexClient,
    cerebras_pivot_gate_response_format,
    cerebras_ready_gate_response_format,
    cerebras_stage_response_format,
    cerebras_structured_response_format,
    parse_bos_eos_text,
    parse_json_suggestion,
)
from .schemas import (
    ChatRequest,
    HelpRequest,
    InterviewAnswerRequest,
    InterviewQuestionRequest,
    InterviewQuestionResponse,
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
from .scorecard import (
    fallback_scorecard,
    fallback_next_action,
    is_speakable_next_action,
    normalize_scorecard,
    scorecard_advice_prompt,
    safe_parse_scorecard,
    scorecard_system_prompt,
)
from .stage_assets import (
    CURRENT_STAGE_AGENDA_PROMPT,
    STAGE_AGENDA_BY_TAG,
    KNOWN_STAGES,
    clamp_stage_forward,
    normalize_stage,
    parse_stage_detection,
    stage_is_backward,
    stage_detection_system_prompt,
)

INTERVIEW_MAX_SENTENCES = 7
INTERVIEW_MAX_OUTPUT_TOKENS = 220
INTERVIEW_MAX_OUTPUT_CHARS = 900
INTERVIEW_SENTENCE_BOUNDARY = re.compile(r'[.!?](?:["”’\']+)?\s+')


logger = logging.getLogger("uvicorn.error")
HELP_TEMPERATURE = 1.0
STAGE_SCORECARD_TIMEOUT_SECS = 10.0
STAGE_CEREBRAS_SCORECARD_TIMEOUT_SECS = 3.0
STAGE_ADVICE_DELAY_SECS = 1.5
STAGE_ADVICE_TIMEOUT_SECS = 3.0

FALLBACK_HELP_OPENER_TEXT = (
    "Давайте зафиксируем главный риск и разберем, что должно быть иначе, "
    "чтобы вам было безопасно двигаться дальше."
)


async def limit_interview_answer_stream(
    stream: AsyncIterator[str],
    *,
    max_sentences: int = INTERVIEW_MAX_SENTENCES,
    max_chars: int = INTERVIEW_MAX_OUTPUT_CHARS,
) -> AsyncIterator[str]:
    pending = ""
    emitted_sentences = 0
    emitted_chars = 0

    def compact_piece(value: str) -> str:
        return " ".join(value.split())

    def fit_piece(value: str, prefix: str) -> tuple[str, bool]:
        room = max_chars - emitted_chars - len(prefix)
        if room <= 0:
            return "", True
        if len(value) <= room:
            return prefix + value, False
        clipped = value[:room].rsplit(" ", 1)[0].rstrip(" ,;:-.!?")
        if not clipped:
            return "", True
        return prefix + clipped + ".", True

    async for delta in stream:
        pending += delta
        while emitted_sentences < max_sentences:
            boundary = INTERVIEW_SENTENCE_BOUNDARY.search(pending)
            if boundary is None:
                break
            piece = compact_piece(pending[: boundary.end()])
            pending = pending[boundary.end() :]
            if not piece:
                continue
            prefix = "" if emitted_sentences == 0 else " "
            output, clipped = fit_piece(piece, prefix)
            if output:
                yield output
                emitted_chars += len(output)
                emitted_sentences += 1
            if clipped or emitted_sentences >= max_sentences:
                return

        if emitted_chars + len(pending) > max_chars:
            piece = compact_piece(pending)
            prefix = "" if emitted_sentences == 0 else " "
            output, _ = fit_piece(piece, prefix)
            if output:
                yield output
            return

    if emitted_sentences < max_sentences:
        piece = compact_piece(pending)
        if piece:
            prefix = "" if emitted_sentences == 0 else " "
            output, _ = fit_piece(piece, prefix)
            if output:
                yield output


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
        if client is not None:
            self.client = client
            self.cerebras_client = client
            self.openrouter_client = client
            self.vertex_client = client
            self._owns_client = False
            self._owns_cerebras_client = False
            self._owns_openrouter_client = False
        else:
            self.vertex_client = self._new_http_client(settings)
            if settings.outbound_proxy:
                self.cerebras_client = self._new_http_client(
                    settings,
                    proxy=settings.outbound_proxy,
                )
            else:
                self.cerebras_client = self.vertex_client
            if settings.openrouter_proxy:
                self.openrouter_client = self._new_http_client(
                    settings,
                    proxy=settings.openrouter_proxy,
                )
            else:
                self.openrouter_client = self.vertex_client
            self.client = self.vertex_client
            self._owns_client = True
            self._owns_cerebras_client = self.cerebras_client is not self.vertex_client
            self._owns_openrouter_client = self.openrouter_client is not self.vertex_client

        self.cerebras = CerebrasClient(settings, self.cerebras_client)
        self.openrouter = OpenRouterClient(settings, self.openrouter_client)
        self.vertex = VertexClient(settings, self.vertex_client)
        self._opener_cooldowns: dict[str, float] = {}
        self._live_intelligence_sessions: dict[str, VertexLiveIntelligenceSession] = {}

    @staticmethod
    def _new_http_client(
        settings: Settings,
        proxy: str | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_secs, pool=2.0),
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
            proxy=proxy,
        )

    async def aclose(self) -> None:
        if self._live_intelligence_sessions:
            await asyncio.gather(
                *(session.aclose() for session in self._live_intelligence_sessions.values()),
                return_exceptions=True,
            )
            self._live_intelligence_sessions.clear()
        if self._owns_cerebras_client:
            await self.cerebras_client.aclose()
        if self._owns_openrouter_client:
            await self.openrouter_client.aclose()
        if self._owns_client:
            await self.vertex_client.aclose()

    def status(self) -> str:
        return "ready" if self._any_provider_configured() else "disabled"

    async def live(self, request: LiveRequest) -> LiveResponse:
        started_at = time.monotonic()
        current_text = (request.current_text or "").strip()

        if self._split_live_flow():
            if current_text and not request.force:
                try:
                    validator_started_at = time.monotonic()
                    verdict = await self._cerebras_live_validator(request)
                    validator_elapsed_ms = int(
                        (time.monotonic() - validator_started_at) * 1000
                    )
                    logger.info(
                        "live_validator run_id=%s action=%s elapsed_ms=%s chars=%s current_chars=%s",
                        request.run_id,
                        verdict["action"],
                        validator_elapsed_ms,
                        len(request.content),
                        len(current_text),
                    )
                    if verdict["action"] == "skip":
                        logger.info(
                            "live_response run_id=%s action=skip provider=cerebras model=%s total_elapsed_ms=%s",
                            request.run_id,
                            self.settings.cerebras_model,
                            int((time.monotonic() - started_at) * 1000),
                        )
                        return LiveResponse(
                            action="skip",
                            text="",
                            provider="cerebras",
                            model=self.settings.cerebras_model,
                        )
                except (ProviderError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "live_validator error run_id=%s provider=cerebras model=%s error=%s",
                        request.run_id,
                        self.settings.cerebras_model,
                        exc,
                    )

            return await self._live_parallel_generate(request, started_at)

        provider, model, suggestion = await self._live_single_provider(request)
        logger.info(
            "live_response run_id=%s action=%s provider=%s model=%s total_elapsed_ms=%s",
            request.run_id,
            suggestion["action"],
            provider,
            model,
            int((time.monotonic() - started_at) * 1000),
        )
        return LiveResponse(
            action=suggestion["action"],
            text=suggestion["text"],
            provider=provider,
            model=model,
        )

    async def ready_gate(self, request: ReadyGateRequest) -> ReadyGateResponse:
        started_at = time.monotonic()
        if not self.cerebras.configured():
            response = fallback_ready_gate(request)
            logger.info(
                "ready_gate fallback run_id=%s revision=%s action=%s reason=no_cerebras",
                request.run_id,
                request.client_revision,
                response.action,
            )
            return response

        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_READY_GATE_SYSTEM_PROMPT,
            user_content=ready_gate_user_content(request),
            temperature=0.0,
            prompt_cache_key=f"rec-sidecar-ready-gate-v1-{request.run_id}",
            max_tokens=420,
            response_format=cerebras_ready_gate_response_format(),
        )
        response = parse_ready_gate_response(
            text,
            fallback_revision=request.client_revision,
            provider="cerebras",
            model=self.settings.cerebras_model,
        )
        logger.info(
            "ready_gate run_id=%s revision=%s action=%s confidence=%.2f elapsed_ms=%s model=%s",
            request.run_id,
            response.client_revision,
            response.action,
            response.confidence,
            int((time.monotonic() - started_at) * 1000),
            self.settings.cerebras_model,
        )
        return response

    async def pivot_gate(self, request: PivotGateRequest) -> PivotGateResponse:
        started_at = time.monotonic()
        if not self.cerebras.configured():
            response = fallback_pivot_gate(request)
            logger.info(
                "pivot_gate fallback run_id=%s revision=%s status=%s reason=no_cerebras",
                request.run_id,
                request.client_revision,
                response.status,
            )
            return response

        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_PIVOT_GATE_SYSTEM_PROMPT,
            user_content=pivot_gate_user_content(request),
            temperature=0.0,
            prompt_cache_key=f"rec-sidecar-pivot-gate-v1-{request.run_id}",
            max_tokens=460,
            response_format=cerebras_pivot_gate_response_format(),
        )
        response = parse_pivot_gate_response(
            text,
            fallback_revision=request.client_revision,
            provider="cerebras",
            model=self.settings.cerebras_model,
        )
        logger.info(
            "pivot_gate run_id=%s revision=%s status=%s confidence=%.2f elapsed_ms=%s model=%s",
            request.run_id,
            response.client_revision,
            response.status,
            response.confidence,
            int((time.monotonic() - started_at) * 1000),
            self.settings.cerebras_model,
        )
        return response

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[bytes]:
        user_content = f"{request.context}\n\n--- Вопрос продавца ---\n{request.question}\n"
        if self._prefer_gemini() or not self.cerebras.configured():
            async for event in self._gemini_text_stream(
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
            if exc.is_rate_limit and self._auto_provider() and self._gemini_text_configured():
                async for event in self._gemini_text_stream(
                    system_prompt=SALES_COACH_CHAT_SYSTEM_PROMPT,
                    user_content=user_content,
                    temperature=0.35,
                ):
                    yield event
            else:
                yield sse_event({"event": "error", "message": str(exc)})

    async def student_translate(
        self, request: StudentTranslateRequest
    ) -> StudentTranslateResponse:
        source, target = ("English", "Russian")
        if request.direction == "ru-en":
            source, target = ("Russian", "English")
        user_content = (
            f"Direction: {source} -> {target}\n\n"
            f"Text:\n{request.text.strip()}\n"
        )
        if self.cerebras.configured():
            try:
                text = await self.cerebras.text(
                    model=self.settings.student_translation_model,
                    system_prompt=STUDENT_TRANSLATION_SYSTEM_PROMPT,
                    user_content=user_content,
                    temperature=0.0,
                    prompt_cache_key=f"rec-sidecar-student-translate-v1-{request.run_id}",
                    max_tokens=1200,
                )
                return StudentTranslateResponse(
                    text=text.strip(),
                    provider="cerebras",
                    model=self.settings.student_translation_model,
                )
            except ProviderError as exc:
                logger.warning(
                    "student_translate_cerebras_fallback run_id=%s status=%s error=%s",
                    request.run_id,
                    exc.status_code,
                    str(exc)[:240],
                )
                if not self._gemini_text_configured():
                    raise

        if not self._gemini_text_configured():
            raise ProviderError("student_translation", "no configured translation provider")

        parts: list[str] = []
        async for delta in self._gemini_stream_text(
            model=self._gemini_text_model(),
            system_prompt=STUDENT_TRANSLATION_SYSTEM_PROMPT,
            user_content=user_content,
            temperature=0.0,
            thinking_level=self.settings.vertex_thinking_level,
        ):
            parts.append(delta)
        text = "".join(parts).strip()
        if not text:
            raise ProviderError(self._gemini_text_provider(), "empty student translation response")
        return StudentTranslateResponse(
            text=text,
            provider=self._gemini_text_provider(),
            model=self._gemini_text_model(),
        )

    async def student_answer_stream(
        self, request: StudentAnswerRequest
    ) -> AsyncIterator[bytes]:
        if not self._gemini_text_configured():
            yield sse_event({"event": "error", "message": "gemini: missing text provider auth"})
            return
        question = (request.question or "").strip()
        user_content = request.context
        system_prompt = STUDENT_ANSWER_SYSTEM_PROMPT
        if question:
            user_content += f"\n\n--- Question ---\n{question}\n"
        else:
            system_prompt = STUDENT_HELP_SYSTEM_PROMPT
            user_content += "\n\n--- Task ---\nКнопка Помоги: объясни последний фрагмент в строгом формате TL;DR + 1-2 предметных примера.\n"
        try:
            yield sse_event(
                {
                    "event": "model",
                    "model": self._gemini_text_model(),
                    "provider": self._gemini_text_provider(),
                }
            )
            async for delta in self._gemini_stream_text(
                model=self._gemini_text_model(),
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=0.35,
                thinking_level=self.settings.vertex_thinking_level,
            ):
                yield sse_event({"event": "delta", "text": delta})
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            yield sse_event({"event": "error", "message": str(exc)})

    async def interview_question(
        self, request: InterviewQuestionRequest
    ) -> InterviewQuestionResponse:
        if not self._gemini_text_configured():
            raise ProviderError("gemini", "Gemini text backend is not configured")
        user_content = (
            f"{request.context}\n\n"
            "--- Latest system-audio candidate ---\n"
            f"{request.candidate.strip()}\n"
        )
        raw = await self._gemini_generate_stage_detection(
            model=self._gemini_text_model(),
            system_prompt=INTERVIEW_QUESTION_DETECTOR_SYSTEM_PROMPT,
            user_content=user_content,
            temperature=0.0,
            thinking_level="minimal",
        )
        value = load_json_object(raw)
        is_question = value.get("is_question") is True or str(
            value.get("is_question", "")
        ).strip().lower() in {"1", "true", "yes"}
        question = " ".join(str(value.get("question") or "").split())
        if is_question and not question:
            question = " ".join(request.candidate.split())
        return InterviewQuestionResponse(
            is_question=is_question and bool(question),
            question=question if is_question else "",
            provider=self._gemini_text_provider(),
            model=self._gemini_text_model(),
        )

    async def interview_answer_stream(
        self, request: InterviewAnswerRequest
    ) -> AsyncIterator[bytes]:
        if not self._gemini_text_configured():
            yield sse_event(
                {"event": "error", "message": "gemini: missing text provider auth"}
            )
            return
        system_prompt = (
            INTERVIEW_HELP_ANSWER_SYSTEM_PROMPT
            if request.trigger == "help"
            else INTERVIEW_LIVE_ANSWER_SYSTEM_PROMPT
        )
        user_content = (
            f"{request.context}\n\n"
            "--- Interviewer question to answer now ---\n"
            f"{request.question.strip()}\n\n"
            "Draft the ready spoken answer now."
        )
        try:
            yield sse_event(
                {
                    "event": "model",
                    "model": self._gemini_text_model(),
                    "provider": self._gemini_text_provider(),
                }
            )
            raw_stream = self._gemini_stream_text(
                model=self._gemini_text_model(),
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=0.25,
                thinking_level=self.settings.vertex_thinking_level,
                max_tokens=INTERVIEW_MAX_OUTPUT_TOKENS,
            )
            async for delta in limit_interview_answer_stream(raw_stream):
                yield sse_event({"event": "delta", "text": delta})
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            yield sse_event({"event": "error", "message": str(exc)})

    async def stage_agenda(self, request: StageRequest) -> StageAgendaResponse | None:
        stage_started_at = time.monotonic()
        user_content = (
            f"{request.context}\n\n"
            f"--- Текущий stage из предыдущего шага ---\n"
            f"{request.current_stage or '(пока неизвестен)'}\n"
        )
        errors: list[str] = []

        if self._use_live_intelligence() and self.vertex.configured():
            model = self.settings.vertex_live_model
            try:
                return await self._stage_agenda_live(request=request)
            except LiveIntelligenceNoUpdate:
                logger.info(
                    "stage_live_intelligence no_update run_id=%s model=%s",
                    request.run_id,
                    model,
                )
                return None
            except (ProviderError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"vertex-live/{model}: {exc}")
                logger.warning(
                    "stage_live_intelligence provider_error run_id=%s provider=vertex-live model=%s error=%s",
                    request.run_id,
                    model,
                    exc,
                )

        if self.cerebras.configured():
            model = self.settings.cerebras_stage_model
            try:
                detect_started_at = time.monotonic()
                text = await self.cerebras.text(
                    model=model,
                    system_prompt=stage_detection_system_prompt(),
                    user_content=user_content,
                    temperature=0.0,
                    prompt_cache_key=f"rec-sidecar-stage-detect-v2-{request.run_id}",
                    max_tokens=96,
                    response_format=cerebras_stage_response_format(),
                )
                stage, confidence = parse_stage_detection(text)
                stage = self._clamp_detected_stage(request, stage, model)
                if self._stage_unchanged(
                    request, stage, provider="cerebras", model=model
                ) and not request.include_scorecard:
                    return None
                return await self._stage_response(
                    request=request,
                    stage=stage,
                    confidence=confidence,
                    provider="cerebras",
                    model=model,
                    detect_elapsed_ms=int((time.monotonic() - detect_started_at) * 1000),
                )
            except (ProviderError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"cerebras/{model}: {exc}")
                logger.warning(
                    "stage_detect provider_error run_id=%s provider=cerebras model=%s error=%s",
                    request.run_id,
                    model,
                    exc,
                )

        if self._gemini_text_configured():
            provider = self._gemini_text_provider()
            model = self._gemini_text_model()
            try:
                detect_started_at = time.monotonic()
                text = await self._gemini_generate_stage_detection(
                    model=model,
                    system_prompt=stage_detection_system_prompt(),
                    user_content=user_content,
                    temperature=0.0,
                    thinking_level=self.settings.vertex_thinking_level,
                )
                stage, confidence = parse_stage_detection(text)
                stage = self._clamp_detected_stage(request, stage, model)
                if self._stage_unchanged(
                    request, stage, provider=provider, model=model
                ) and not request.include_scorecard:
                    return None
                return await self._stage_response(
                    request=request,
                    stage=stage,
                    confidence=confidence,
                    provider=provider,
                    model=model,
                    detect_elapsed_ms=int((time.monotonic() - detect_started_at) * 1000),
                )
            except (ProviderError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{provider}/{model}: {exc}")
                logger.warning(
                    "stage_detect provider_error run_id=%s provider=%s model=%s error=%s",
                    request.run_id,
                    provider,
                    model,
                    exc,
                )

        fallback_stage = self._fallback_stage(request.current_stage)
        logger.warning(
            "stage_detect fallback run_id=%s stage=%s errors=%s",
            request.run_id,
            fallback_stage,
            " | ".join(errors) or "no provider configured",
        )
        return await self._stage_response(
            request=request,
            stage=fallback_stage,
            confidence=0.0,
            provider="fallback",
            model="last-known-stage",
            detect_elapsed_ms=int((time.monotonic() - stage_started_at) * 1000),
        )

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
        if not self._gemini_text_configured():
            yield sse_event({"event": "done"})
            return

        user_content = (
            f"{request.context}\n\n"
            "--- Fixed stage -> agenda mapping ---\n"
            f"{CURRENT_STAGE_AGENDA_PROMPT}\n\n"
            "--- Задача ---\n"
            "Подготовь один короткий следующий ход продавцу для текущего момента звонка. "
            "Строго опирайся на текущий stage, agenda и stage -> agenda mapping."
        )
        started_at = time.monotonic()
        first_delta_logged = False
        total_chars = 0
        try:
            yield sse_event(
                {
                    "event": "model",
                    "model": self._gemini_text_model(),
                    "provider": self._gemini_text_provider(),
                }
            )
            stripper = ConstructivePrefixStripper()
            async for delta in self._gemini_stream_text(
                system_prompt=SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT,
                user_content=user_content,
                temperature=HELP_TEMPERATURE,
            ):
                delta = stripper.feed(delta)
                if delta:
                    total_chars += len(delta)
                    if not first_delta_logged:
                        first_delta_logged = True
                        logger.info(
                            "help_constructive first_delta id=%s run_id=%s model=%s elapsed_ms=%s chars=%s",
                            request.id,
                            request.run_id,
                            self._gemini_text_model(),
                            int((time.monotonic() - started_at) * 1000),
                            len(delta),
                        )
                    yield sse_event({"event": "delta", "text": delta})
            logger.info(
                "help_constructive done id=%s run_id=%s model=%s elapsed_ms=%s chars=%s",
                request.id,
                request.run_id,
                self._gemini_text_model(),
                int((time.monotonic() - started_at) * 1000),
                total_chars,
            )
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            logger.info(
                "help_constructive error id=%s run_id=%s model=%s elapsed_ms=%s error=%s",
                request.id,
                request.run_id,
                self._gemini_text_model(),
                int((time.monotonic() - started_at) * 1000),
                exc,
            )
            yield sse_event({"event": "error", "message": str(exc)})

    async def _cerebras_live_structured(self, request: LiveRequest) -> dict[str, str]:
        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT,
            user_content=self._live_generator_user_content(request),
            temperature=0.35,
            prompt_cache_key=f"rec-sidecar-sales-coach-live-v1-{request.run_id}",
            response_format=cerebras_structured_response_format(),
        )
        return parse_json_suggestion(text)

    async def _cerebras_live_generate(
        self, request: LiveRequest
    ) -> tuple[str, str, dict[str, str]]:
        try:
            suggestion = await self._cerebras_live_structured(request)
        except ProviderError as exc:
            if not exc.is_structured_output_error:
                raise
            suggestion = await self._cerebras_live_unstructured(request)
        except (ValueError, json.JSONDecodeError):
            suggestion = await self._cerebras_live_unstructured(request)
        if suggestion["action"] != "suggest" or not suggestion["text"].strip():
            raise ProviderError("cerebras", "empty live fallback suggestion")
        return ("cerebras", self.settings.cerebras_model, suggestion)

    async def _live_parallel_generate(
        self, request: LiveRequest, started_at: float
    ) -> LiveResponse:
        primary_task = asyncio.create_task(self._gemini_live_generate(request))
        fallback_task = asyncio.create_task(self._cerebras_live_generate(request))
        try:
            provider, model, suggestion = await primary_task
        except (ProviderError, ValueError, json.JSONDecodeError) as primary_error:
            logger.warning(
                "live_generator error run_id=%s provider=%s model=%s error=%s",
                request.run_id,
                self._gemini_text_provider(),
                self._gemini_text_model(),
                primary_error,
            )
            try:
                provider, model, suggestion = await fallback_task
            except (ProviderError, ValueError, json.JSONDecodeError) as fallback_error:
                logger.warning(
                    "live_generator error run_id=%s provider=cerebras model=%s error=%s",
                    request.run_id,
                    self.settings.cerebras_model,
                    fallback_error,
                )
                raise primary_error from fallback_error
            logger.info(
                "live_response run_id=%s action=%s provider=%s model=%s source=fallback_after_primary_error total_elapsed_ms=%s",
                request.run_id,
                suggestion["action"],
                provider,
                model,
                int((time.monotonic() - started_at) * 1000),
            )
            return LiveResponse(
                action=suggestion["action"],
                text=suggestion["text"],
                provider=provider,
                model=model,
            )
        finally:
            if not fallback_task.done():
                fallback_task.cancel()
            await asyncio.gather(fallback_task, return_exceptions=True)

        logger.info(
            "live_response run_id=%s action=%s provider=%s model=%s source=primary total_elapsed_ms=%s",
            request.run_id,
            suggestion["action"],
            provider,
            model,
            int((time.monotonic() - started_at) * 1000),
        )
        return LiveResponse(
            action=suggestion["action"],
            text=suggestion["text"],
            provider=provider,
            model=model,
        )

    async def _cerebras_live_validator(self, request: LiveRequest) -> dict[str, str]:
        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_LIVE_VALIDATOR_SYSTEM_PROMPT,
            user_content=self._live_validator_user_content(request),
            temperature=0.0,
            prompt_cache_key=f"rec-sidecar-sales-coach-validator-v1-{request.run_id}",
            response_format=cerebras_structured_response_format(),
        )
        return parse_json_suggestion(text)

    async def _cerebras_live_unstructured(self, request: LiveRequest) -> dict[str, str]:
        text = await self.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT,
            user_content=self._live_generator_user_content(request),
            temperature=0.35,
            prompt_cache_key=f"rec-sidecar-sales-coach-live-v1-{request.run_id}",
        )
        return parse_json_suggestion(text)

    async def _vertex_live(self, request: LiveRequest) -> dict[str, str]:
        return (await self._gemini_live_generate(request))[2]

    async def _vertex_live_generate(self, request: LiveRequest) -> dict[str, str]:
        return (await self._gemini_live_generate(request))[2]

    async def _gemini_live_generate(
        self, request: LiveRequest
    ) -> tuple[str, str, dict[str, str]]:
        if not self._gemini_text_configured():
            raise ProviderError("gemini", "Gemini text backend is not configured")
        suggestion = await self._gemini_generate_structured(
            model=self._gemini_text_model(),
            system_prompt=SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT,
            user_content=self._live_generator_user_content(request),
            temperature=0.35,
        )
        if suggestion["action"] != "suggest" or not suggestion["text"].strip():
            raise ProviderError(self._gemini_text_provider(), "empty live generator suggestion")
        return (self._gemini_text_provider(), self._gemini_text_model(), suggestion)

    async def _live_single_provider(
        self, request: LiveRequest
    ) -> tuple[str, str, dict[str, str]]:
        if self._prefer_gemini():
            suggestion = await self._vertex_live(request)
            return (self._gemini_text_provider(), self._gemini_text_model(), suggestion)

        if not self.cerebras.configured():
            if self._gemini_text_configured():
                suggestion = await self._vertex_live(request)
                return (self._gemini_text_provider(), self._gemini_text_model(), suggestion)
            raise ProviderError("service", "no LLM provider configured")

        try:
            suggestion = await self._cerebras_live_structured(request)
        except ProviderError as exc:
            if exc.is_structured_output_error:
                suggestion = await self._cerebras_live_unstructured(request)
            elif self._auto_provider() and self._gemini_text_configured():
                suggestion = await self._vertex_live(request)
                return (self._gemini_text_provider(), self._gemini_text_model(), suggestion)
            else:
                raise
        except (ValueError, json.JSONDecodeError):
            suggestion = await self._cerebras_live_unstructured(request)

        return ("cerebras", self.settings.cerebras_model, suggestion)

    def _live_validator_user_content(self, request: LiveRequest) -> str:
        current_text = (request.current_text or "").strip() or "(текущей реплики пока нет)"
        return (
            "--- Текущая реплика на экране ---\n"
            f"{current_text}\n\n"
            "--- Свежий снимок звонка ---\n"
            f"{request.content}\n"
        )

    def _live_generator_user_content(self, request: LiveRequest) -> str:
        current_text = (request.current_text or "").strip()
        parts = [
            "--- Свежий снимок звонка ---",
            request.content,
            "",
            "--- Как использовать методологию ---",
            "Сначала опирайся на живой диалог и последнюю осмысленную реплику клиента. Current stage / agenda и Current scorecard используй как контроль: не слишком ли долго топчемся на стадии, не ушли ли не туда, какой следующий факт полезно добрать. Если клиент уже согласился отвечать или прямо просит перейти к сути, не повторяй permission/рамку и сразу задай первый содержательный вопрос.",
        ]
        if current_text:
            parts.extend(
                [
                    "",
                    "--- Текущая реплика на экране ---",
                    current_text,
                    "Если она устарела, дай более уместную свежую замену, а не косметический рерайт.",
                ]
            )
        if request.force:
            parts.extend(
                [
                    "",
                    "--- Обновление принудительно запрошено ---",
                    "Сгенерируй свежую реплику под текущий момент разговора.",
                ]
            )
        return "\n".join(parts)

    async def _stage_response(
        self,
        *,
        request: StageRequest,
        stage: str,
        confidence: float | None,
        provider: str,
        model: str,
        detect_elapsed_ms: int | None = None,
    ) -> StageAgendaResponse:
        response_started_at = time.monotonic()
        agenda = STAGE_AGENDA_BY_TAG[stage]
        logger.info(
            "stage_detect run_id=%s stage=%s provider=%s model=%s confidence=%s elapsed_ms=%s",
            request.run_id,
            stage,
            provider,
            model,
            confidence,
            detect_elapsed_ms,
        )
        scorecard = await self._stage_scorecard(request, agenda) if request.include_scorecard else None
        response_elapsed_ms = int((time.monotonic() - response_started_at) * 1000)
        total_elapsed_ms = (
            detect_elapsed_ms + response_elapsed_ms
            if detect_elapsed_ms is not None
            else response_elapsed_ms
        )
        logger.info(
            "stage_response run_id=%s stage=%s total_elapsed_ms=%s post_detect_elapsed_ms=%s include_scorecard=%s",
            request.run_id,
            stage,
            total_elapsed_ms,
            response_elapsed_ms,
            request.include_scorecard,
        )
        return StageAgendaResponse(
            stage=agenda.stage,
            title=agenda.title,
            agenda=agenda.agenda,
            emotion=agenda.emotion,
            step=agenda.step,
            provider=provider,
            model=model,
            confidence=confidence,
            scorecard=scorecard,
        )

    async def _stage_agenda_live(self, *, request: StageRequest) -> StageAgendaResponse:
        started_at = time.monotonic()
        session = self._live_intelligence_session(request.run_id)
        result = await session.analyze(
            context=request.context,
            current_stage=request.current_stage,
        )
        stage = self._clamp_detected_stage(
            request, result.stage, self.settings.vertex_live_model
        )
        agenda = STAGE_AGENDA_BY_TAG[stage]
        scorecard = normalize_scorecard(
            stage=agenda.stage,
            agenda=agenda,
            raw=result.scorecard,
            context=request.context,
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "stage_live_intelligence run_id=%s stage=%s model=%s confidence=%s readiness=%s score=%s hits=%s misses=%s elapsed_ms=%s",
            request.run_id,
            agenda.stage,
            self.settings.vertex_live_model,
            result.confidence,
            scorecard.readiness,
            scorecard.score,
            scorecard.hit_count,
            scorecard.miss_count,
            elapsed_ms,
        )
        return StageAgendaResponse(
            stage=agenda.stage,
            title=agenda.title,
            agenda=agenda.agenda,
            emotion=agenda.emotion,
            step=agenda.step,
            provider="vertex-live",
            model=self.settings.vertex_live_model,
            confidence=result.confidence,
            scorecard=scorecard,
        )

    def _live_intelligence_session(self, run_id: str) -> VertexLiveIntelligenceSession:
        session = self._live_intelligence_sessions.get(run_id)
        if session is None:
            session = VertexLiveIntelligenceSession(
                self.vertex,
                model=self.settings.vertex_live_model,
                timeout_secs=self.settings.vertex_live_timeout_secs,
            )
            self._live_intelligence_sessions[run_id] = session
        return session

    def _fallback_stage(self, current_stage: str | None) -> str:
        stage = normalize_stage(current_stage or "")
        if stage in STAGE_AGENDA_BY_TAG:
            return stage
        return KNOWN_STAGES[0]

    def _stage_unchanged(
        self,
        request: StageRequest,
        proposed_stage: str,
        *,
        provider: str,
        model: str,
    ) -> bool:
        current_stage = normalize_stage(request.current_stage or "")
        if not current_stage or current_stage != proposed_stage:
            return False
        logger.info(
            "stage_detect no_change run_id=%s stage=%s provider=%s model=%s",
            request.run_id,
            proposed_stage,
            provider,
            model,
        )
        return True

    def _clamp_detected_stage(
        self, request: StageRequest, proposed_stage: str, model: str
    ) -> str:
        if stage_is_backward(request.current_stage, proposed_stage):
            logger.info(
                "stage_detect backward_stage_ignored run_id=%s current_stage=%s incoming_stage=%s model=%s",
                request.run_id,
                request.current_stage,
                proposed_stage,
                model,
            )
        return clamp_stage_forward(request.current_stage, proposed_stage)

    async def _stage_scorecard(self, request: StageRequest, agenda) -> object:
        if not self._gemini_text_configured():
            return fallback_scorecard(
                agenda.stage,
                agenda,
                "Scorecard evaluator disabled: Gemini text backend is not configured.",
                context=request.context,
            )

        started_at = time.monotonic()
        provider = self._gemini_text_provider()
        model = self._gemini_text_model()
        user_content = (
            f"{request.context}\n\n"
            f"--- Текущий stage из предыдущего шага ---\n"
            f"{request.current_stage or '(пока неизвестен)'}\n"
        )
        advice_prompt = scorecard_advice_prompt(agenda.stage, agenda)
        scorecard_task = asyncio.create_task(
            self._gemini_generate_scorecard(
                model=model,
                system_prompt=scorecard_system_prompt(agenda.stage, agenda),
                user_content=user_content,
                temperature=0.0,
                thinking_level=self.settings.vertex_scorecard_thinking_level,
            )
        )
        advice_task: asyncio.Task[str] | None = None

        def start_advice_task() -> asyncio.Task[str]:
            return asyncio.create_task(
                self._stage_advice_text(
                    system_prompt=advice_prompt,
                    user_content=user_content,
                )
            )

        try:
            done, _ = await asyncio.wait(
                {scorecard_task},
                timeout=STAGE_ADVICE_DELAY_SECS,
            )
            if not done:
                advice_task = start_advice_task()
            remaining_timeout = max(
                0.1,
                STAGE_SCORECARD_TIMEOUT_SECS - (time.monotonic() - started_at),
            )
            text = await asyncio.wait_for(scorecard_task, remaining_timeout)
            raw = safe_parse_scorecard(text)
            scorecard = normalize_scorecard(
                stage=agenda.stage,
                agenda=agenda,
                raw=raw,
                context=request.context,
            )
            logger.info(
                "stage_scorecard run_id=%s stage=%s provider=%s model=%s readiness=%s score=%s hits=%s misses=%s elapsed_ms=%s",
                request.run_id,
                agenda.stage,
                provider,
                model,
                scorecard.readiness,
                scorecard.score,
                scorecard.hit_count,
                scorecard.miss_count,
                int((time.monotonic() - started_at) * 1000),
            )
            return scorecard
        except (ProviderError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            reason = scorecard_error_reason(exc)
            cerebras_scorecard = await self._stage_scorecard_cerebras(
                request=request,
                agenda=agenda,
                user_content=user_content,
                started_at=started_at,
            )
            if cerebras_scorecard is not None:
                return cerebras_scorecard
            advice_started_after_error = advice_task is None
            if advice_task is None:
                advice_task = start_advice_task()
            advice = await self._scorecard_advice_from_task(
                advice_task,
                timeout=STAGE_ADVICE_TIMEOUT_SECS if advice_started_after_error else 0.5,
            )
            if not advice:
                advice = await self._stage_advice_cerebras(
                    request=request,
                    system_prompt=advice_prompt,
                    user_content=user_content,
                )
            advice = pending_safe_advice(advice, agenda.step)
            logger.warning(
                "stage_scorecard fallback run_id=%s stage=%s provider=%s model=%s elapsed_ms=%s error=%s advice=%s",
                request.run_id,
                agenda.stage,
                provider,
                model,
                int((time.monotonic() - started_at) * 1000),
                reason,
                bool(advice),
            )
            return fallback_scorecard(
                agenda.stage,
                agenda,
                f"Scorecard evaluator fallback: {reason}",
                next_action=advice,
                context=request.context,
            )
        finally:
            await cancel_tasks(
                *[task for task in (scorecard_task, advice_task) if task is not None]
            )

    async def _stage_scorecard_cerebras(
        self, *, request: StageRequest, agenda, user_content: str, started_at: float
    ) -> object | None:
        if not self.cerebras.configured():
            return None
        model = self.settings.help_opener_secondary_model
        try:
            text = await asyncio.wait_for(
                self.cerebras.text(
                    model=model,
                    system_prompt=scorecard_system_prompt(agenda.stage, agenda),
                    user_content=user_content,
                    temperature=0.0,
                    prompt_cache_key=f"rec-sidecar-stage-scorecard-v1-{request.run_id}",
                ),
                timeout=STAGE_CEREBRAS_SCORECARD_TIMEOUT_SECS,
            )
            raw = safe_parse_scorecard(text)
            scorecard = normalize_scorecard(
                stage=agenda.stage,
                agenda=agenda,
                raw=raw,
                context=request.context,
            )
            logger.info(
                "stage_scorecard fallback_provider=cerebras run_id=%s stage=%s model=%s readiness=%s score=%s hits=%s misses=%s elapsed_ms=%s",
                request.run_id,
                agenda.stage,
                model,
                scorecard.readiness,
                scorecard.score,
                scorecard.hit_count,
                scorecard.miss_count,
                int((time.monotonic() - started_at) * 1000),
            )
            return scorecard
        except (ProviderError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            logger.info(
                "stage_scorecard cerebras_fallback_failed run_id=%s stage=%s model=%s elapsed_ms=%s error=%s",
                request.run_id,
                agenda.stage,
                model,
                int((time.monotonic() - started_at) * 1000),
                scorecard_error_reason(exc),
            )
            return None

    async def _scorecard_advice_from_task(
        self, task: asyncio.Task[str], *, timeout: float
    ) -> str | None:
        try:
            text = await asyncio.wait_for(task, timeout)
        except (ProviderError, TimeoutError) as exc:
            logger.info("stage_scorecard advice_fallback error=%s", scorecard_error_reason(exc))
            return None
        text = clean_one_line(text)
        return text if is_usable_advice(text) else None

    async def _stage_advice_text(self, *, system_prompt: str, user_content: str) -> str:
        parts: list[str] = []
        started_at = time.monotonic()
        async for delta in self._gemini_stream_text(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.4,
            thinking_level=self.settings.vertex_thinking_level,
        ):
            parts.append(delta)
            text = clean_one_line("".join(parts))
            if len(text) >= 90 or text.endswith((".", "?", "!")):
                logger.info(
                    "stage_scorecard advice_ready elapsed_ms=%s chars=%s",
                    int((time.monotonic() - started_at) * 1000),
                    len(text),
                )
                return text
            if time.monotonic() - started_at >= 2.0 and is_usable_advice(text):
                logger.info(
                    "stage_scorecard advice_partial elapsed_ms=%s chars=%s",
                    int((time.monotonic() - started_at) * 1000),
                    len(text),
                )
                return text
        text = clean_one_line("".join(parts))
        return text if is_usable_advice(text) else ""

    async def _stage_advice_cerebras(
        self, *, request: StageRequest, system_prompt: str, user_content: str
    ) -> str | None:
        if not self.cerebras.configured():
            return None
        started_at = time.monotonic()
        model = self.settings.help_opener_secondary_model
        try:
            text = await self.cerebras.text(
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=0.4,
                prompt_cache_key=f"rec-sidecar-stage-advice-v1-{request.run_id}",
            )
        except ProviderError as exc:
            logger.info(
                "stage_scorecard advice_cerebras_error run_id=%s model=%s elapsed_ms=%s error=%s",
                request.run_id,
                model,
                int((time.monotonic() - started_at) * 1000),
                exc,
            )
            return None
        text = clean_one_line(text)
        if not is_usable_advice(text):
            return None
        logger.info(
            "stage_scorecard advice_cerebras_ready run_id=%s model=%s elapsed_ms=%s chars=%s",
            request.run_id,
            model,
            int((time.monotonic() - started_at) * 1000),
            len(text),
        )
        return text or None

    def _gemini_text_configured(self) -> bool:
        return self.openrouter.configured() or self.vertex.configured()

    def _gemini_text_provider(self) -> str:
        return "openrouter" if self.openrouter.configured() else "vertex"

    def _gemini_text_model(self) -> str:
        if self.openrouter.configured():
            return self.settings.openrouter_gemini_model
        return self.settings.vertex_model

    async def _gemini_generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> dict[str, str]:
        if self.openrouter.configured():
            return await self.openrouter.generate_structured(
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            )
        return await self.vertex.generate_structured(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            thinking_level=self.settings.vertex_thinking_level,
        )

    async def _gemini_generate_stage_detection(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        if self.openrouter.configured():
            return await self.openrouter.generate_stage_detection(
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            )
        return await self.vertex.generate_stage_detection(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            thinking_level=thinking_level,
        )

    async def _gemini_generate_scorecard(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        if self.openrouter.configured():
            return await self.openrouter.generate_scorecard(
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            )
        return await self.vertex.generate_scorecard(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            thinking_level=thinking_level,
        )

    def _gemini_stream_text(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self.openrouter.configured():
            return self.openrouter.stream_text(
                model=model or self.settings.openrouter_gemini_model,
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return self.vertex.stream_text(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            thinking_level=thinking_level,
            max_tokens=max_tokens,
        )

    async def _gemini_text_stream(
        self,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> AsyncIterator[bytes]:
        try:
            yield sse_event(
                {
                    "event": "model",
                    "model": self._gemini_text_model(),
                    "provider": self._gemini_text_provider(),
                }
            )
            async for delta in self._gemini_stream_text(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            ):
                yield sse_event({"event": "delta", "text": delta})
            yield sse_event({"event": "done"})
        except ProviderError as exc:
            yield sse_event({"event": "error", "message": str(exc)})

    async def _vertex_text_stream(
        self,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> AsyncIterator[bytes]:
        async for event in self._gemini_text_stream(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
        ):
            yield event

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
            "прочитать клиенту вслух. Не добавляй вопрос, оффер или следующий шаг."
        )
        candidates: list[OpenerCandidate] = []

        use_cerebras = self.settings.provider == "cerebras" or self._auto_provider()
        use_vertex = self._prefer_gemini() or self._auto_provider()

        gemini_slot = self._gemini_text_provider()
        vertex_model = self._ready_opener_model(gemini_slot, self._gemini_text_model())
        if use_vertex and self._gemini_text_configured() and vertex_model:
            candidates.append(
                OpenerCandidate(
                    slot=gemini_slot,
                    priority=0,
                    provider=gemini_slot,
                    model=vertex_model,
                    stream=self._gemini_stream_text(
                        model=vertex_model,
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

    def _prefer_gemini(self) -> bool:
        return self.settings.provider in {"vertex", "gemini", "google", "openrouter"}

    def _prefer_vertex(self) -> bool:
        return self._prefer_gemini()

    def _auto_provider(self) -> bool:
        return self.settings.provider not in {
            "cerebras",
            "vertex",
            "gemini",
            "google",
            "openrouter",
        }

    def _split_live_flow(self) -> bool:
        return (
            self._auto_provider()
            and self.cerebras.configured()
            and self._gemini_text_configured()
        )

    def _use_live_intelligence(self) -> bool:
        return self.settings.intelligence_transport in {"live", "websocket", "gemini-live"}

    def _any_provider_configured(self) -> bool:
        if self._prefer_gemini():
            return self._gemini_text_configured()
        if self.settings.provider == "cerebras":
            return self.cerebras.configured()
        return self.cerebras.configured() or self._gemini_text_configured()


def ready_gate_user_content(request: ReadyGateRequest) -> str:
    current_text = (request.current_text or "").strip() or "(empty)"
    return (
        f"{request.content.strip()}\n\n"
        f"--- Current visible seller reply ---\n{current_text}\n\n"
        f"--- Client revision ---\n{request.client_revision}\n"
    )


def pivot_gate_user_content(request: PivotGateRequest) -> str:
    current_text = (request.current_text or "").strip() or "(empty)"
    active_generation_id = (request.active_generation_id or "").strip() or "(unknown)"
    base_client_text = (request.base_client_text or "").strip() or "(empty)"
    pending_replan_state = (request.pending_replan_state or "").strip() or "pending_replan=false; level=none"
    return (
        f"{request.content.strip()}\n\n"
        f"--- Current visible seller reply ---\n{current_text}\n\n"
        f"--- Active Gemini generation ---\n{active_generation_id}\n\n"
        f"--- Base client text used to start Gemini ---\n{base_client_text}\n\n"
        f"--- Pending replan state ---\n{pending_replan_state}\n\n"
        f"--- Client revision ---\n{request.client_revision}\n"
    )


def parse_ready_gate_response(
    text: str,
    *,
    fallback_revision: int,
    provider: str,
    model: str,
) -> ReadyGateResponse:
    value = load_json_object(text)
    action = normalize_ready_action(value.get("action"))
    readiness = normalize_ready_readiness(value.get("readiness"), action)
    semantic_type = normalize_ready_semantic_type(value.get("semantic_type"))
    mutex_decision = "LOCK_AND_GENERATE" if action == "GENERATE" else "DO_NOT_LOCK"
    return ReadyGateResponse(
        client_revision=positive_int(value.get("client_revision"), fallback_revision),
        action=action,
        confidence=clamp_confidence(value.get("confidence")),
        reason=str(value.get("reason") or "").strip(),
        readiness=readiness,
        semantic_type=semantic_type,
        mutex_decision=mutex_decision,
        generation_brief=str(value.get("generation_brief") or "").strip(),
        latest_client_intent=str(value.get("latest_client_intent") or "").strip(),
        provider=provider,
        model=model,
    )


def parse_pivot_gate_response(
    text: str,
    *,
    fallback_revision: int,
    provider: str,
    model: str,
) -> PivotGateResponse:
    value = load_json_object(text)
    status = normalize_pivot_status(value.get("status"))
    pivot_type = normalize_pivot_type(value.get("pivot_type"))
    sets_pending_replan = status == "CHANGE_HARD"
    clears_pending_replan = status == "NO_CHANGE"
    replan_level = "hard" if status == "CHANGE_HARD" else "soft" if status == "ADAPT_SOFT" else "none"
    return PivotGateResponse(
        client_revision=positive_int(value.get("client_revision"), fallback_revision),
        status=status,
        confidence=clamp_confidence(value.get("confidence")),
        reason=str(value.get("reason") or "").strip(),
        pivot_type=pivot_type,
        sets_pending_replan=sets_pending_replan,
        clears_pending_replan=clears_pending_replan,
        replan_level=replan_level,
        latest_client_intent=str(value.get("latest_client_intent") or "").strip(),
        base_client_intent=str(value.get("base_client_intent") or "").strip(),
        provider=provider,
        model=model,
    )


def fallback_ready_gate(request: ReadyGateRequest) -> ReadyGateResponse:
    has_context = bool(request.content.strip())
    has_current = bool((request.current_text or "").strip())
    action = "WAIT"
    readiness = "incomplete"
    reason = "fallback ready gate waits for clearer client intent"
    if has_context and not has_current:
        action = "GENERATE"
        readiness = "actionable"
        reason = "fallback ready gate has context and no visible reply"
    elif has_context and has_current:
        action = "KEEP"
        readiness = "meaningful_but_covered"
        reason = "fallback ready gate keeps current visible reply"
    return ReadyGateResponse(
        client_revision=request.client_revision,
        action=action,
        confidence=1.0,
        reason=reason,
        readiness=readiness,
        semantic_type="other",
        mutex_decision="LOCK_AND_GENERATE" if action == "GENERATE" else "DO_NOT_LOCK",
        generation_brief="",
        latest_client_intent="",
        provider="fallback",
        model="local",
    )


def fallback_pivot_gate(request: PivotGateRequest) -> PivotGateResponse:
    return PivotGateResponse(
        client_revision=request.client_revision,
        status="WAIT_NOISE",
        confidence=1.0,
        reason="fallback pivot gate leaves pending replan unchanged",
        pivot_type="none",
        sets_pending_replan=False,
        clears_pending_replan=False,
        replan_level="none",
        latest_client_intent="",
        base_client_intent="",
        provider="fallback",
        model="local",
    )


def load_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("structured gate response must be a JSON object")
    return value


def positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return parsed if parsed > 0 else fallback


def clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def normalize_ready_action(value: Any) -> str:
    action = str(value or "").strip().upper()
    if action in {"SUGGEST", "ON"}:
        return "GENERATE"
    if action == "SKIP":
        return "KEEP"
    if action in {"WAIT", "KEEP", "GENERATE"}:
        return action
    return "WAIT"


def normalize_ready_readiness(value: Any, action: str) -> str:
    readiness = str(value or "").strip().lower()
    if readiness in {"incomplete", "noise", "meaningful_but_covered", "actionable"}:
        return readiness
    if action == "GENERATE":
        return "actionable"
    if action == "KEEP":
        return "meaningful_but_covered"
    return "incomplete"


def normalize_ready_semantic_type(value: Any) -> str:
    semantic_type = str(value or "").strip().lower()
    if semantic_type in {
        "none",
        "question",
        "objection",
        "concern",
        "buying_signal",
        "price",
        "budget",
        "timing",
        "integration",
        "competitor",
        "authority",
        "next_step",
        "correction",
        "refusal",
        "clarification",
        "other",
    }:
        return semantic_type
    return "other"


def normalize_pivot_status(value: Any) -> str:
    status = str(value or "").strip().upper()
    if status in {"SUGGEST", "GENERATE", "INVALIDATED"}:
        return "CHANGE_HARD"
    if status in {"SKIP", "VALID"}:
        return "NO_CHANGE"
    if status == "SOFT":
        return "ADAPT_SOFT"
    if status in {"WAIT_NOISE", "NO_CHANGE", "ADAPT_SOFT", "CHANGE_HARD"}:
        return status
    if status == "WAIT":
        return "WAIT_NOISE"
    return "WAIT_NOISE"


def normalize_pivot_type(value: Any) -> str:
    pivot_type = str(value or "").strip().lower()
    if pivot_type in {
        "none",
        "objection",
        "price",
        "budget",
        "timing",
        "integration",
        "competitor",
        "authority",
        "priority_shift",
        "refusal",
        "correction",
        "new_question",
        "buying_signal",
        "other",
    }:
        return pivot_type
    return "none"


def sse_event(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {text}\n\n".encode("utf-8")


def normalize_opener_delta(delta: str, first: bool = False) -> str:
    if first:
        return delta.lstrip().lstrip("\"'«»“”")
    return delta


CONSTRUCTIVE_PREFIX_MARKERS = (
    "**следующий ход:**",
    "следующий ход:",
    "**следующий шаг:**",
    "следующий шаг:",
)


class ConstructivePrefixStripper:
    def __init__(self) -> None:
        self._buffer = ""
        self._settled = False

    def feed(self, delta: str) -> str:
        if self._settled:
            return delta

        self._buffer += delta
        stripped = strip_constructive_prefix(self._buffer)
        if stripped != self._buffer:
            self._settled = True
            self._buffer = ""
            return stripped

        if could_be_constructive_prefix(self._buffer):
            return ""

        self._settled = True
        text = self._buffer
        self._buffer = ""
        return text


def strip_constructive_prefix(text: str) -> str:
    candidate = constructive_prefix_candidate(text)
    lower = candidate.lower()
    for marker in CONSTRUCTIVE_PREFIX_MARKERS:
        if lower.startswith(marker):
            return candidate[len(marker) :].lstrip(" \t\r\n:-—–")
    return text


def could_be_constructive_prefix(text: str) -> bool:
    candidate = constructive_prefix_candidate(text).lower()
    return bool(candidate) and any(
        marker.startswith(candidate) for marker in CONSTRUCTIVE_PREFIX_MARKERS
    )


def constructive_prefix_candidate(text: str) -> str:
    candidate = text.lstrip()
    while candidate.startswith(">"):
        candidate = candidate[1:].lstrip()
    return candidate


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


def clean_one_line(text: str) -> str:
    return " ".join(text.strip().strip("\"'`").split())


def is_usable_advice(text: str) -> bool:
    text = text.strip()
    return len(text) >= 32 and (len(text) >= 90 or text.endswith((".", "?", "!")))


def scorecard_error_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return f"timeout after {STAGE_SCORECARD_TIMEOUT_SECS:.0f}s"
    return str(exc) or exc.__class__.__name__


def pending_safe_advice(advice: str | None, fallback_step: str) -> str | None:
    if not advice:
        return None
    prefix = ""
    body = advice
    for candidate_prefix in ("Уточнить:", "Переход:"):
        if advice.startswith(candidate_prefix):
            prefix = candidate_prefix
            body = advice[len(candidate_prefix) :].strip()
            break
    if advice.lower().startswith("переход:"):
        return f"Уточнить: {fallback_step}"
    if not is_speakable_next_action(body):
        return fallback_next_action()
    if prefix:
        return advice
    return advice


async def cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
