from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response

logger = logging.getLogger("uvicorn.error")

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except Exception:  # pragma: no cover - optional local dev fallback
    CONTENT_TYPE_LATEST = "text/plain"
    Counter = Gauge = Histogram = None  # type: ignore[assignment]

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except Exception:  # pragma: no cover - optional local dev fallback
    trace = None  # type: ignore[assignment]
    OTLPSpanExporter = None  # type: ignore[assignment]
    FastAPIInstrumentor = None  # type: ignore[assignment]
    HTTPXClientInstrumentor = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]


HTTP_LATENCY = (
    Histogram(
        "llm_http_request_duration_ms",
        "LLM sidecar HTTP request duration in milliseconds",
        ["method", "path", "status"],
        buckets=(50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000),
    )
    if Histogram
    else None
)
LLM_LATENCY = (
    Histogram(
        "llm_request_duration_ms",
        "LLM provider request duration in milliseconds",
        ["provider", "model", "mode"],
        buckets=(50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000),
    )
    if Histogram
    else None
)
LLM_ERRORS = (
    Counter("llm_errors_total", "LLM provider errors", ["provider", "model", "mode", "error_type"])
    if Counter
    else None
)
LLM_INFLIGHT = (
    Gauge("llm_inflight_requests", "LLM inflight provider requests", ["provider", "model", "mode"])
    if Gauge
    else None
)


def setup_observability(app: FastAPI) -> None:
    setup_tracing(app)
    setup_metrics(app)


def setup_tracing(app: FastAPI) -> None:
    if not trace or not TracerProvider or not OTLPSpanExporter:
        logger.warning("otel python packages unavailable; traces disabled")
        return
    endpoint = otlp_grpc_endpoint_from_env()
    if not endpoint:
        logger.info("otel traces disabled; OTEL_EXPORTER_OTLP_ENDPOINT is empty")
        return
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "llm-sidecar"),
            "deployment.environment": os.getenv("DEPLOY_ENV", "local"),
            "service.version": os.getenv("GIT_SHA", "dev"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    if FastAPIInstrumentor:
        FastAPIInstrumentor.instrument_app(app, excluded_urls=os.getenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "/healthz,/metrics"))
    if HTTPXClientInstrumentor:
        HTTPXClientInstrumentor().instrument()
    logger.info("otel traces enabled service=%s endpoint=%s", os.getenv("OTEL_SERVICE_NAME", "llm-sidecar"), endpoint)


def otlp_grpc_endpoint_from_env() -> str:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return normalize_otlp_grpc_endpoint(endpoint)


def normalize_otlp_grpc_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return parsed.netloc
    return endpoint.removeprefix("http://").removeprefix("https://")


def setup_metrics(app: FastAPI) -> None:
    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        status = "500"
        try:
            response: Response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            if HTTP_LATENCY:
                elapsed_ms = (time.perf_counter() - started) * 1000
                HTTP_LATENCY.labels(request.method, request.url.path, status).observe(elapsed_ms)

    @app.get("/metrics")
    async def metrics() -> Response:
        if not generate_latest:
            return Response("# prometheus_client unavailable\n", media_type="text/plain")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@contextmanager
def provider_timer(provider: str, model: str, mode: str) -> Iterator[None]:
    if LLM_INFLIGHT:
        LLM_INFLIGHT.labels(provider, model, mode).inc()
    started = time.perf_counter()
    try:
        yield
    except Exception:
        if LLM_ERRORS:
            LLM_ERRORS.labels(provider, model, mode, "exception").inc()
        raise
    finally:
        if LLM_INFLIGHT:
            LLM_INFLIGHT.labels(provider, model, mode).dec()
        if LLM_LATENCY:
            LLM_LATENCY.labels(provider, model, mode).observe((time.perf_counter() - started) * 1000)


def current_trace_id() -> str:
    if not trace:
        return ""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context or not context.is_valid:
        return ""
    return format(context.trace_id, "032x")
