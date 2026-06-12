import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest

from llm_service.app.config import Settings
from llm_service.app.orchestrator import LlmOrchestrator, OpenerCandidate, sse_event
from llm_service.app.providers import (
    CerebrasClient,
    parse_bos_eos_text,
    parse_json_suggestion,
    pop_vertex_stream_value,
)
from llm_service.app.schemas import HelpRequest


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(**overrides):
    values = {
        "provider": "auto",
        "service_token": None,
        "outbound_proxy": None,
        "timeout_secs": 30.0,
        "rate_limit_backoff_ms": 15_000,
        "help_opener_timeout_ms": 4_000,
        "cerebras_api_key": "test-key",
        "cerebras_api_base": "https://cerebras.test/v1",
        "cerebras_model": "zai-glm-4.7",
        "help_opener_primary_model": "primary-model",
        "help_opener_secondary_model": "secondary-model",
        "cerebras_reasoning_effort": "none",
        "cerebras_prompt_cache_key": True,
        "vertex_project": None,
        "vertex_location": "global",
        "vertex_model": "gemini-3.5-flash",
        "vertex_api_base": "https://aiplatform.googleapis.com",
        "vertex_access_token": None,
        "vertex_adc_credentials_path": None,
        "vertex_quota_project_id": None,
        "vertex_thinking_level": "low",
    }
    values.update(overrides)
    return Settings(**values)


def test_suggestion_parsers():
    assert parse_json_suggestion('{"action":"suggest","text":"Спроси про цель."}') == {
        "action": "suggest",
        "text": "Спроси про цель.",
    }
    assert parse_bos_eos_text("EOS") == {"action": "skip", "text": ""}
    assert parse_bos_eos_text("BOS Уточни бюджет.") == {
        "action": "suggest",
        "text": "Уточни бюджет.",
    }


@pytest.mark.anyio
async def test_cerebras_prompt_cache_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "prompt_cache_key" in payload:
            return httpx.Response(400, text="prompt_cache_key unsupported")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        cerebras = CerebrasClient(make_settings(), client)

        text = await cerebras.text(
            model="model",
            system_prompt="system",
            user_content="user",
            temperature=0.2,
            prompt_cache_key="cache-key",
        )

        assert text == "ok"
        assert len(calls) == 2
        assert "prompt_cache_key" in calls[0]
        assert "prompt_cache_key" not in calls[1]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_selects_primary_model():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model = payload["model"]
        text = (
            'data: {"choices":[{"delta":{"content":"primary answer"}}]}\n\n'
            "data: [DONE]\n\n"
            if model == "primary-model"
            else "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=text)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(make_settings(), client)

        response = await orchestrator.help_opener(
            HelpRequest(id=1, run_id="run", context="context")
        )

        assert response.text == "primary answer"
        assert response.model == "primary-model"
        assert response.fallback is False
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_stream_waits_for_higher_priority_delta():
    async def high_priority_stream() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "priority answer"

    async def low_priority_stream() -> AsyncIterator[str]:
        yield "fast"
        yield " answer"

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(make_settings(cerebras_api_key=None), client)
        orchestrator._opener_candidates = lambda _: [
            OpenerCandidate(
                slot="gemini",
                priority=0,
                provider="vertex",
                model="gemini-3.5-flash",
                stream=high_priority_stream(),
            ),
            OpenerCandidate(
                slot="oss",
                priority=2,
                provider="cerebras",
                model="gpt-oss-120b",
                stream=low_priority_stream(),
            ),
        ]

        frames = [
            json.loads(frame.decode("utf-8").removeprefix("data:").strip())
            async for frame in orchestrator.help_opener_stream(
                HelpRequest(id=1, run_id="run", context="context")
            )
        ]

        assert frames == [
            {"event": "model", "model": "gemini-3.5-flash", "provider": "vertex"},
            {"event": "delta", "text": "priority answer"},
            {"event": "done"},
        ]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_stream_uses_lower_priority_after_timeout():
    async def high_priority_stream() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "too late"

    async def low_priority_stream() -> AsyncIterator[str]:
        yield "fallback winner"

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(cerebras_api_key=None, help_opener_timeout_ms=5), client
        )
        orchestrator._opener_candidates = lambda _: [
            OpenerCandidate(
                slot="gemini",
                priority=0,
                provider="vertex",
                model="gemini-3.5-flash",
                stream=high_priority_stream(),
            ),
            OpenerCandidate(
                slot="oss",
                priority=2,
                provider="cerebras",
                model="gpt-oss-120b",
                stream=low_priority_stream(),
            ),
        ]

        frames = [
            json.loads(frame.decode("utf-8").removeprefix("data:").strip())
            async for frame in orchestrator.help_opener_stream(
                HelpRequest(id=1, run_id="run", context="context")
            )
        ]

        assert frames == [
            {"event": "model", "model": "gpt-oss-120b", "provider": "cerebras"},
            {"event": "delta", "text": "fallback winner"},
            {"event": "done"},
        ]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_vertex_candidate_sends_low_thinking():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='[{"candidates":[{"content":{"parts":[{"text":"vertex answer"}]}}]}]',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                vertex_project="project",
                vertex_access_token="token",
                vertex_thinking_level="low",
            ),
            client,
        )

        response = await orchestrator.help_opener(
            HelpRequest(id=1, run_id="run", context="context")
        )

        assert response.text == "vertex answer"
        assert response.model == "gemini-3.5-flash"
        assert calls[0]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low"
        }
    finally:
        await client.aclose()


def test_sse_event_is_json_data_frame():
    frame = sse_event({"event": "delta", "text": "привет"}).decode("utf-8")

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert '"text":"привет"' in frame


def test_vertex_stream_parser_handles_array_delimited_json():
    buffer = (
        '[{"candidates":[{"content":{"parts":[{"text":"one"}]}}]},'
        '{"candidates":[{"content":{"parts":[{"text":"two"}]}}]}]'
    )

    first, buffer, consumed = pop_vertex_stream_value(buffer)
    assert consumed is True
    assert first["candidates"][0]["content"]["parts"][0]["text"] == "one"

    second, buffer, consumed = pop_vertex_stream_value(buffer)
    assert consumed is True
    assert second["candidates"][0]["content"]["parts"][0]["text"] == "two"

    _, _, consumed = pop_vertex_stream_value(buffer)
    assert consumed is False
