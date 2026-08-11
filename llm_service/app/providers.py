from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from .config import GOOGLE_OAUTH_TOKEN_URL, Settings
from .telemetry import provider_timer


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status_code: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.message = message

    @property
    def is_rate_limit(self) -> bool:
        text = self.message.lower()
        return self.status_code == 429 or any(
            marker in text
            for marker in (
                "too many requests",
                "rate limit",
                "rate_limit",
                "quota",
                "resource_exhausted",
                "exceeded",
            )
        )

    @property
    def is_prompt_cache_error(self) -> bool:
        text = self.message.lower()
        return self.status_code in {400, 403} and any(
            marker in text for marker in ("prompt_cache_key", "cache", "routing hint")
        )

    @property
    def is_structured_output_error(self) -> bool:
        text = self.message.lower()
        return self.status_code == 400 and any(
            marker in text for marker in ("response_format", "json_schema", "schema", "structured")
        )


def value_to_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = "".join(part for item in value if (part := value_to_text(item)))
        return text or None
    if isinstance(value, dict):
        return value_to_text(value.get("text")) or value_to_text(value.get("content"))
    return None


def response_content(value: dict[str, Any]) -> str | None:
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if isinstance(message, dict):
        text = value_to_text(message.get("content"))
        if text:
            return text
        for key in ("reasoning", "reasoning_content", "reasoningContent"):
            reasoning = value_to_text(message.get(key))
            if reasoning and (json_text := extract_json_text(reasoning)):
                return json_text
    return value_to_text(choice.get("text")) or value_to_text(choice.get("content"))


def extract_json_text(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or start >= end:
        return None
    candidate = text[start : end + 1]
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    return json.dumps(value, ensure_ascii=False)


def stream_content_parts(value: dict[str, Any]) -> list[str]:
    choices = value.get("choices")
    if not isinstance(choices, list):
        return []

    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = value_to_text(delta.get("content"))
            if text:
                parts.append(text)
            elif reasoning := value_to_text(
                delta.get("reasoning_content") or delta.get("reasoningContent")
            ):
                parts.append(reasoning)
        message = choice.get("message")
        if isinstance(message, dict):
            text = value_to_text(message.get("content"))
            if text:
                parts.append(text)
        for key in ("text", "content"):
            text = value_to_text(choice.get(key))
            if text:
                parts.append(text)
    return parts


def vertex_response_text(value: dict[str, Any]) -> str | None:
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    return text if text.strip() else None


def vertex_function_call_args(value: dict[str, Any], name: str) -> dict[str, Any] | None:
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            function_call = part.get("functionCall") or part.get("function_call")
            if not isinstance(function_call, dict) or function_call.get("name") != name:
                continue
            args = function_call.get("args")
            if isinstance(args, dict):
                return args
    return None


def vertex_live_response_text(value: dict[str, Any]) -> str | None:
    server_content = value.get("serverContent") or value.get("server_content")
    if not isinstance(server_content, dict):
        return None
    model_turn = server_content.get("modelTurn") or server_content.get("model_turn")
    if not isinstance(model_turn, dict):
        return None
    parts = model_turn.get("parts")
    if not isinstance(parts, list):
        return None
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    return text if text.strip() else None


def vertex_live_turn_complete(value: dict[str, Any]) -> bool:
    server_content = value.get("serverContent") or value.get("server_content")
    if not isinstance(server_content, dict):
        return False
    return bool(server_content.get("turnComplete") or server_content.get("turn_complete"))


def vertex_live_error_message(value: dict[str, Any]) -> str | None:
    error = value.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("status") or error.get("code")
        return str(message) if message else compact_json(error)
    if isinstance(error, str):
        return error
    go_away = value.get("goAway") or value.get("go_away")
    if isinstance(go_away, dict):
        return f"goAway: {compact_json(go_away)}"
    return None


def cerebras_structured_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sales_coach_suggestion",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["skip", "suggest"]},
                    "text": {"type": "string"},
                },
                "required": ["action", "text"],
                "additionalProperties": False,
            },
        },
    }


def cerebras_ready_gate_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sales_ready_gate",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "client_revision": {"type": "integer"},
                    "action": {"type": "string", "enum": ["WAIT", "KEEP", "GENERATE"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "readiness": {
                        "type": "string",
                        "enum": [
                            "incomplete",
                            "noise",
                            "meaningful_but_covered",
                            "actionable",
                        ],
                    },
                    "semantic_type": {
                        "type": "string",
                        "enum": [
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
                        ],
                    },
                    "mutex_decision": {
                        "type": "string",
                        "enum": ["DO_NOT_LOCK", "LOCK_AND_GENERATE"],
                    },
                    "generation_brief": {"type": "string"},
                    "latest_client_intent": {"type": "string"},
                },
                "required": [
                    "client_revision",
                    "action",
                    "confidence",
                    "reason",
                    "readiness",
                    "semantic_type",
                    "mutex_decision",
                    "generation_brief",
                    "latest_client_intent",
                ],
                "additionalProperties": False,
            },
        },
    }


def cerebras_pivot_gate_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sales_pivot_gate",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "client_revision": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["NO_CHANGE", "WAIT_NOISE", "ADAPT_SOFT", "CHANGE_HARD"],
                    },
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "pivot_type": {
                        "type": "string",
                        "enum": [
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
                        ],
                    },
                    "sets_pending_replan": {"type": "boolean"},
                    "clears_pending_replan": {"type": "boolean"},
                    "replan_level": {"type": "string", "enum": ["none", "soft", "hard"]},
                    "latest_client_intent": {"type": "string"},
                    "base_client_intent": {"type": "string"},
                },
                "required": [
                    "client_revision",
                    "status",
                    "confidence",
                    "reason",
                    "pivot_type",
                    "sets_pending_replan",
                    "clears_pending_replan",
                    "replan_level",
                    "latest_client_intent",
                    "base_client_intent",
                ],
                "additionalProperties": False,
            },
        },
    }


def cerebras_stage_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sales_stage_detection",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "enum": [
                            "S2.1",
                            "S2.2",
                            "S2.3",
                            "S2.4",
                            "S2.5",
                            "S3.1",
                            "S3.2",
                            "S3.3",
                            "S3.4a",
                            "S3.4b",
                            "S3.5",
                        ],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["stage", "confidence"],
                "additionalProperties": False,
            },
        },
    }


def vertex_coach_response_schema() -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["skip", "suggest"]},
            "text": {"type": "STRING"},
        },
        "required": ["action", "text"],
    }


def vertex_stage_response_schema() -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {
            "stage": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["stage"],
    }


def vertex_stage_function_declaration() -> dict[str, Any]:
    return {
        "name": "submit_stage_detection",
        "description": "Submit the current sales conversation stage as structured data.",
        "parameters": vertex_stage_response_schema(),
    }


def vertex_scorecard_function_declaration() -> dict[str, Any]:
    return {
        "name": "submit_scorecard",
        "description": (
            "Submit the current sales-stage scorecard and tactical next action for the seller."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "summary": {
                    "type": "STRING",
                    "description": "Short summary of readiness for the current stage.",
                },
                "next_action": {
                    "type": "STRING",
                    "description": (
                        "One short seller action. Start with Уточнить: or Переход:."
                    ),
                },
                "checks": {
                    "type": "ARRAY",
                    "description": "Scorecard checks for the current stage only.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "id": {"type": "STRING"},
                            "result": {
                                "type": "STRING",
                                "description": "hit, miss, pending, uncertain, or na.",
                            },
                            "reason": {"type": "STRING"},
                            "quote": {
                                "type": "STRING",
                                "description": "Optional short evidence quote.",
                            },
                        },
                        "required": ["id", "result", "reason"],
                    },
                },
            },
            "required": ["summary", "next_action", "checks"],
        },
    }


def parse_json_suggestion(text: str) -> dict[str, str]:
    value = json.loads(text)
    action = value.get("action")
    if action not in {"skip", "suggest"}:
        raise ValueError(f"unexpected action: {action!r}")
    response_text = value.get("text", "")
    if not isinstance(response_text, str):
        raise ValueError("suggestion text must be a string")
    return {"action": action, "text": response_text}


def parse_bos_eos_text(text: str) -> dict[str, str]:
    stripped = text.strip()
    if not stripped or stripped.startswith("EOS"):
        return {"action": "skip", "text": ""}
    if stripped.startswith("BOS"):
        stripped = stripped[3:].lstrip()
    return {"action": "suggest", "text": stripped}


def strip_outer_quotes(text: str) -> str:
    return text.strip().strip("\"'«»“”").strip()


class CerebrasClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    def configured(self) -> bool:
        return self.settings.cerebras_configured

    def _headers(self) -> dict[str, str]:
        if not self.settings.cerebras_api_key:
            raise ProviderError("cerebras", "missing CEREBRAS_API_KEY")
        return {"Authorization": f"Bearer {self.settings.cerebras_api_key}"}

    def _body(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        stream: bool,
        prompt_cache_key: str | None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "stream": stream,
        }
        effort = self.settings.cerebras_reasoning_effort.strip()
        if effort.lower() == "none" and model.startswith("zai-"):
            body["reasoning_effort"] = "none"
        elif effort and effort.lower() != "none":
            body["reasoning_effort"] = effort
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if self.settings.cerebras_prompt_cache_key and prompt_cache_key:
            body["prompt_cache_key"] = prompt_cache_key
        if response_format:
            body["response_format"] = response_format
        return body

    async def text(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        prompt_cache_key: str | None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body = self._body(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            stream=False,
            prompt_cache_key=prompt_cache_key,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        try:
            value = await self._post_json(body)
        except ProviderError as exc:
            if body.get("prompt_cache_key") and exc.is_prompt_cache_error:
                body.pop("prompt_cache_key", None)
                value = await self._post_json(body)
            else:
                raise

        text = response_content(value)
        if not text or not text.strip():
            raise ProviderError("cerebras", f"empty text response: {compact_json(value)}")
        return text

    async def stream_text(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        prompt_cache_key: str | None,
    ) -> AsyncIterator[str]:
        body = self._body(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            stream=True,
            prompt_cache_key=prompt_cache_key,
        )
        try:
            async for delta in self._stream_json_deltas(body):
                yield delta
        except ProviderError as exc:
            if body.get("prompt_cache_key") and exc.is_prompt_cache_error:
                body.pop("prompt_cache_key", None)
                async for delta in self._stream_json_deltas(body):
                    yield delta
            else:
                raise

    async def _post_json(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.cerebras_api_base.rstrip('/')}/chat/completions"
        try:
            with provider_timer("cerebras", str(body.get("model") or ""), "generic"):
                response = await self.client.post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise ProviderError("cerebras", f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise ProviderError("cerebras", response.text, response.status_code)
        return response.json()

    async def _stream_json_deltas(self, body: dict[str, Any]) -> AsyncIterator[str]:
        url = f"{self.settings.cerebras_api_base.rstrip('/')}/chat/completions"
        try:
            with provider_timer("cerebras", str(body.get("model") or ""), "stream"):
                async with self.client.stream(
                    "POST", url, headers=self._headers(), json=body
                ) as response:
                    if not response.is_success:
                        text = (await response.aread()).decode("utf-8", errors="replace")
                        raise ProviderError("cerebras", text, response.status_code)

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            return
                        try:
                            value = json.loads(data)
                        except ValueError:
                            continue
                        for part in stream_content_parts(value):
                            if part:
                                yield part
        except httpx.HTTPError as exc:
            raise ProviderError("cerebras", f"{exc.__class__.__name__}: {exc}") from exc


class OpenRouterClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    def configured(self) -> bool:
        return self.settings.openrouter_configured

    def _headers(self) -> dict[str, str]:
        if not self.settings.openrouter_api_key:
            raise ProviderError("openrouter", "missing OPENROUTER_API_KEY")
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_app_name:
            headers["X-Title"] = self.settings.openrouter_app_name
        return headers

    def _body(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        stream: bool,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format
        return body

    async def generate_structured(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> dict[str, str]:
        text = await self.text(
            model=model or self.settings.openrouter_gemini_model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            response_format=cerebras_structured_response_format(),
        )
        return parse_json_suggestion(text)

    async def generate_stage_detection(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        return await self.text(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=96,
        )

    async def generate_scorecard(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        return await self.text(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=2048,
        )

    async def text(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body = self._body(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            stream=False,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        try:
            with provider_timer("openrouter", model, "generic"):
                response = await self.client.post(
                    f"{self.settings.openrouter_api_base.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ProviderError("openrouter", f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise ProviderError("openrouter", response.text, response.status_code)
        text = response_content(response.json())
        if not text or not text.strip():
            raise ProviderError("openrouter", f"empty text response: {compact_json(response.json())}")
        return text

    async def stream_text(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        effective_model = model or self.settings.openrouter_gemini_model
        body = self._body(
            model=effective_model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            stream=True,
            max_tokens=max_tokens,
        )
        try:
            with provider_timer("openrouter", effective_model, "stream"):
                async with self.client.stream(
                    "POST",
                    f"{self.settings.openrouter_api_base.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response:
                    if not response.is_success:
                        text = (await response.aread()).decode("utf-8", errors="replace")
                        raise ProviderError("openrouter", text, response.status_code)

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            return
                        try:
                            value = json.loads(data)
                        except ValueError:
                            continue
                        for part in stream_content_parts(value):
                            if part:
                                yield part
        except httpx.HTTPError as exc:
            raise ProviderError("openrouter", f"{exc.__class__.__name__}: {exc}") from exc


class VertexClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._cached_token: tuple[str, float] | None = None

    def configured(self) -> bool:
        return self.settings.vertex_configured

    async def generate_structured(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> dict[str, str]:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": vertex_coach_response_schema(),
        }
        effective_thinking_level = (
            self.settings.vertex_thinking_level
            if thinking_level is None
            else thinking_level
        )
        if effective_thinking_level:
            generation_config["thinkingConfig"] = {
                "thinkingLevel": effective_thinking_level
            }
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        try:
            response = await self.client.post(
                self._method_url_for_model("generateContent", model or self.settings.vertex_model),
                headers=await self._headers(),
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("vertex", f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise ProviderError("vertex", response.text, response.status_code)
        text = vertex_response_text(response.json())
        if not text:
            raise ProviderError("vertex", "empty structured response")
        return parse_json_suggestion(text)

    async def generate_stage_detection(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": 96,
        }
        if thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        try:
            with provider_timer("vertex", model, "scorecard"):
                response = await self.client.post(
                    self._method_url_for_model("generateContent", model),
                    headers=await self._headers(),
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ProviderError("vertex", f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise ProviderError("vertex", response.text, response.status_code)
        value = response.json()
        text = vertex_response_text(value)
        if text:
            return text
        raise ProviderError("vertex", f"empty stage text response: {compact_json(value)}")

    async def generate_scorecard(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> str:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": 2048,
        }
        effective_thinking_level = (
            self.settings.vertex_scorecard_thinking_level
            if thinking_level is None
            else thinking_level
        )
        if effective_thinking_level:
            generation_config["thinkingConfig"] = {
                "thinkingLevel": effective_thinking_level
            }
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        try:
            response = await self.client.post(
                self._method_url_for_model("generateContent", model),
                headers=await self._headers(),
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("vertex", f"{exc.__class__.__name__}: {exc}") from exc
        if not response.is_success:
            raise ProviderError("vertex", response.text, response.status_code)
        value = response.json()
        text = vertex_response_text(value)
        if text:
            return text
        raise ProviderError("vertex", f"empty scorecard text response: {compact_json(value)}")

    async def stream_text(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        generation_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        effective_model = model or self.settings.vertex_model
        with provider_timer("vertex", effective_model, "stream"):
            async with self.client.stream(
                "POST",
                self._method_url_for_model("streamGenerateContent", effective_model),
                headers=await self._headers(),
                json=body,
            ) as response:
                if not response.is_success:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise ProviderError("vertex", text, response.status_code)

                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while True:
                        value, buffer, consumed = pop_vertex_stream_value(buffer)
                        if not consumed:
                            break
                        if value is None:
                            continue
                        text = vertex_response_text(value)
                        if text:
                            yield text

                while True:
                    value, buffer, consumed = pop_vertex_stream_value(buffer)
                    if not consumed:
                        break
                    if value is None:
                        continue
                    text = vertex_response_text(value)
                    if text:
                        yield text

    async def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        if self.settings.vertex_quota_project_id:
            headers["x-goog-user-project"] = self.settings.vertex_quota_project_id
        return headers

    async def auth_headers(self) -> dict[str, str]:
        return await self._headers()

    async def _access_token(self) -> str:
        if self.settings.vertex_access_token:
            return self.settings.vertex_access_token
        if self._cached_token and time.monotonic() < self._cached_token[1]:
            return self._cached_token[0]
        credentials = self._read_adc_credentials()
        params = {
            "grant_type": "refresh_token",
            "client_id": required_adc_field(credentials, "client_id"),
            "client_secret": required_adc_field(credentials, "client_secret"),
            "refresh_token": required_adc_field(credentials, "refresh_token"),
        }
        response = await self.client.post(GOOGLE_OAUTH_TOKEN_URL, data=params)
        if not response.is_success:
            raise ProviderError("vertex", f"ADC token HTTP {response.status_code}: {response.text}")
        value = response.json()
        token = value["access_token"]
        ttl = max(int(value.get("expires_in", 3600)) - 60, 60)
        self._cached_token = (token, time.monotonic() + ttl)
        return token

    def _read_adc_credentials(self) -> dict[str, Any]:
        path = self.settings.vertex_adc_credentials_path
        if not path:
            raise ProviderError("vertex", "missing Vertex auth")
        try:
            return json.loads(Path(path).expanduser().read_text())
        except OSError as exc:
            raise ProviderError("vertex", f"cannot read ADC credentials: {exc}") from exc
        except ValueError as exc:
            raise ProviderError("vertex", "invalid ADC credentials JSON") from exc

    def _method_url(self, method: str) -> str:
        if not self.settings.vertex_project:
            raise ProviderError("vertex", "missing GOOGLE_CLOUD_PROJECT")
        return self._method_url_for_model(method, self.settings.vertex_model)

    def _method_url_for_model(self, method: str, model: str) -> str:
        if not self.settings.vertex_project:
            raise ProviderError("vertex", "missing GOOGLE_CLOUD_PROJECT")
        return (
            f"{self.settings.vertex_api_base.rstrip('/')}/v1/projects/"
            f"{self.settings.vertex_project}/locations/{self.settings.vertex_location}"
            f"/publishers/google/models/{model}:{method}"
        )

    def model_resource(self, model: str) -> str:
        if not self.settings.vertex_project:
            raise ProviderError("vertex", "missing GOOGLE_CLOUD_PROJECT")
        return (
            f"projects/{self.settings.vertex_project}/locations/{self.settings.vertex_location}"
            f"/publishers/google/models/{model}"
        )

    def live_bidi_url(self) -> str:
        return (
            f"wss://{vertex_api_host(self.settings.vertex_api_base)}"
            "/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
        )


def required_adc_field(credentials: dict[str, Any], key: str) -> str:
    value = credentials.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError("vertex", f"ADC credentials missing {key}")
    return value


def vertex_api_host(api_base: str) -> str:
    parsed = urlparse(api_base)
    if parsed.netloc:
        return parsed.netloc
    return api_base.removeprefix("https://").removeprefix("http://").rstrip("/")


def pop_vertex_stream_value(buffer: str) -> tuple[dict[str, Any] | None, str, bool]:
    buffer = discard_vertex_stream_delimiters(buffer)
    if not buffer:
        return None, buffer, False

    if buffer.startswith("data:") or buffer.startswith("event:"):
        event, rest, complete = take_sse_event(buffer)
        if not complete:
            return None, buffer, False
        for data in sse_data_lines(event):
            data = data.strip()
            if not data or data == "[DONE]":
                continue
            return json.loads(data), rest, True
        return None, rest, True

    if not buffer.startswith("{"):
        return None, buffer, False
    end = complete_json_object_end(buffer)
    if end is None:
        return None, buffer, False
    return json.loads(buffer[:end]), buffer[end:], True


def discard_vertex_stream_delimiters(buffer: str) -> str:
    index = 0
    while index < len(buffer) and (buffer[index].isspace() or buffer[index] in "[],"):
        index += 1
    return buffer[index:]


def take_sse_event(buffer: str) -> tuple[str, str, bool]:
    lf = buffer.find("\n\n")
    crlf = buffer.find("\r\n\r\n")
    if lf == -1 and crlf == -1:
        return "", buffer, False
    if crlf != -1 and (lf == -1 or crlf < lf):
        return buffer[:crlf], buffer[crlf + 4 :], True
    return buffer[:lf], buffer[lf + 2 :], True


def sse_data_lines(event: str) -> list[str]:
    lines = []
    for line in event.splitlines():
        line = line.lstrip()
        if line.startswith("data:"):
            lines.append(line.removeprefix("data:").lstrip())
    return lines


def complete_json_object_end(buffer: str) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(buffer):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)[:1200]
