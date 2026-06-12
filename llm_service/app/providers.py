from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .config import GOOGLE_OAUTH_TOKEN_URL, Settings


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
    return value_to_text(choice.get("text")) or value_to_text(choice.get("content"))


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
        if effort and effort.lower() != "none":
            body["reasoning_effort"] = effort
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
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body = self._body(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            stream=False,
            prompt_cache_key=prompt_cache_key,
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
        response = await self.client.post(url, headers=self._headers(), json=body)
        if not response.is_success:
            raise ProviderError("cerebras", response.text, response.status_code)
        return response.json()

    async def _stream_json_deltas(self, body: dict[str, Any]) -> AsyncIterator[str]:
        url = f"{self.settings.cerebras_api_base.rstrip('/')}/chat/completions"
        async with self.client.stream("POST", url, headers=self._headers(), json=body) as response:
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
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> dict[str, str]:
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": vertex_coach_response_schema(),
            },
        }
        response = await self.client.post(
            self._method_url("generateContent"),
            headers=await self._headers(),
            json=body,
        )
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
            "responseMimeType": "application/json",
            "responseSchema": vertex_stage_response_schema(),
        }
        if thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        response = await self.client.post(
            self._method_url_for_model("generateContent", model),
            headers=await self._headers(),
            json=body,
        )
        if not response.is_success:
            raise ProviderError("vertex", response.text, response.status_code)
        text = vertex_response_text(response.json())
        if not text:
            raise ProviderError("vertex", "empty stage response")
        return text

    async def stream_text(
        self,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
        thinking_level: str | None = None,
    ) -> AsyncIterator[str]:
        generation_config: dict[str, Any] = {"temperature": temperature}
        if thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        async with self.client.stream(
            "POST",
            self._method_url("streamGenerateContent"),
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


def required_adc_field(credentials: dict[str, Any], key: str) -> str:
    value = credentials.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError("vertex", f"ADC credentials missing {key}")
    return value


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
