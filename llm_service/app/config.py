from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CEREBRAS_API_BASE = "https://api.cerebras.ai/v1"
DEFAULT_CEREBRAS_MODEL = "zai-glm-4.7"
DEFAULT_HELP_OPENER_PRIMARY_MODEL = "zai-glm-4.7"
DEFAULT_HELP_OPENER_SECONDARY_MODEL = "gpt-oss-120b"
DEFAULT_VERTEX_MODEL = "gemini-3.5-flash"
DEFAULT_TIMEOUT_SECS = 30.0
DEFAULT_RATE_LIMIT_BACKOFF_MS = 15_000
DEFAULT_HELP_OPENER_TIMEOUT_MS = 4_000
DEFAULT_REASONING_EFFORT = "none"
DEFAULT_VERTEX_THINKING_LEVEL = "low"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def env_var(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_bool(name: str, default: bool) -> bool:
    value = env_var(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = env_var(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def default_adc_credentials_path() -> str:
    return "~/.config/gcloud/application_default_credentials.json"


def default_vertex_api_base(location: str) -> str:
    if location.lower() == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{location}-aiplatform.googleapis.com"


@dataclass(frozen=True)
class Settings:
    provider: str
    service_token: str | None
    outbound_proxy: str | None
    timeout_secs: float
    rate_limit_backoff_ms: int
    help_opener_timeout_ms: int

    cerebras_api_key: str | None
    cerebras_api_base: str
    cerebras_model: str
    help_opener_primary_model: str
    help_opener_secondary_model: str
    cerebras_reasoning_effort: str
    cerebras_prompt_cache_key: bool

    vertex_project: str | None
    vertex_location: str
    vertex_model: str
    vertex_api_base: str
    vertex_access_token: str | None
    vertex_adc_credentials_path: str | None
    vertex_quota_project_id: str | None
    vertex_thinking_level: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        vertex_location = env_var("GOOGLE_CLOUD_LOCATION") or env_var("VERTEX_LOCATION") or "global"
        adc_path = (
            env_var("VERTEX_ADC_CREDENTIALS")
            or env_var("GOOGLE_APPLICATION_CREDENTIALS")
            or default_adc_credentials_path()
        )
        vertex_project = (
            env_var("GOOGLE_CLOUD_PROJECT")
            or env_var("VERTEX_PROJECT_ID")
            or quota_project_from_adc(adc_path)
        )
        vertex_api_base = env_var("VERTEX_API_BASE") or default_vertex_api_base(vertex_location)

        return cls(
            provider=(env_var("COACH_PROVIDER") or "auto").lower(),
            service_token=env_var("COACH_LLM_SERVICE_TOKEN"),
            outbound_proxy=env_var("CEREBRAS_PROXY") or env_var("OUTBOUND_PROXY"),
            timeout_secs=float(env_int("CEREBRAS_TIMEOUT_SECS", int(DEFAULT_TIMEOUT_SECS))),
            rate_limit_backoff_ms=env_int(
                "CEREBRAS_RATE_LIMIT_BACKOFF_MS", DEFAULT_RATE_LIMIT_BACKOFF_MS
            ),
            help_opener_timeout_ms=env_int(
                "COACH_HELP_OPENER_TIMEOUT_MS", DEFAULT_HELP_OPENER_TIMEOUT_MS
            ),
            cerebras_api_key=env_var("CEREBRAS_API_KEY"),
            cerebras_api_base=env_var("CEREBRAS_API_BASE") or DEFAULT_CEREBRAS_API_BASE,
            cerebras_model=env_var("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL,
            help_opener_primary_model=(
                env_var("CEREBRAS_HELP_OPENER_PRIMARY_MODEL")
                or env_var("CEREBRAS_HELP_OPENER_FAST_MODEL")
                or DEFAULT_HELP_OPENER_PRIMARY_MODEL
            ),
            help_opener_secondary_model=(
                env_var("CEREBRAS_HELP_OPENER_SECONDARY_MODEL")
                or env_var("CEREBRAS_HELP_OPENER_FALLBACK_MODEL")
                or DEFAULT_HELP_OPENER_SECONDARY_MODEL
            ),
            cerebras_reasoning_effort=(
                env_var("CEREBRAS_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT
            ),
            cerebras_prompt_cache_key=env_bool("CEREBRAS_PROMPT_CACHE_KEY", True),
            vertex_project=vertex_project,
            vertex_location=vertex_location,
            vertex_model=env_var("VERTEX_GEMINI_MODEL")
            or env_var("GEMINI_VERTEX_MODEL")
            or DEFAULT_VERTEX_MODEL,
            vertex_api_base=vertex_api_base,
            vertex_access_token=env_var("VERTEX_ACCESS_TOKEN")
            or env_var("GOOGLE_OAUTH_ACCESS_TOKEN"),
            vertex_adc_credentials_path=adc_path,
            vertex_quota_project_id=env_var("GOOGLE_CLOUD_QUOTA_PROJECT")
            or quota_project_from_adc(adc_path),
            vertex_thinking_level=env_var("VERTEX_THINKING_LEVEL")
            or DEFAULT_VERTEX_THINKING_LEVEL,
        )

    @property
    def cerebras_configured(self) -> bool:
        return bool(self.cerebras_api_key)

    @property
    def vertex_configured(self) -> bool:
        if not self.vertex_project:
            return False
        if self.vertex_access_token:
            return True
        if not self.vertex_adc_credentials_path:
            return False
        return Path(self.vertex_adc_credentials_path).expanduser().exists()

    def provider_label(self) -> str:
        if self.provider == "cerebras":
            return "cerebras" if self.cerebras_configured else "cerebras: disabled"
        if self.provider in {"vertex", "gemini", "google"}:
            return "vertex" if self.vertex_configured else "vertex: disabled"
        if self.cerebras_configured and self.vertex_configured:
            return "auto: cerebras -> vertex"
        if self.cerebras_configured:
            return "auto: cerebras"
        if self.vertex_configured:
            return "auto: vertex"
        return "auto: disabled"

    def active_model_label(self) -> str:
        if self.provider in {"vertex", "gemini", "google"}:
            return self.vertex_model
        if self.provider == "cerebras":
            return self.cerebras_model
        if self.cerebras_configured and self.vertex_configured:
            return (
                f"fast priority {self.vertex_model}({self.vertex_thinking_level or 'default'}) "
                f"-> {self.help_opener_primary_model} -> "
                f"{self.help_opener_secondary_model} / slow {self.vertex_model}"
            )
        if self.vertex_configured:
            return self.vertex_model
        return self.cerebras_model


def quota_project_from_adc(path: str | None) -> str | None:
    if not path:
        return None
    try:
        import json

        text = Path(path).expanduser().read_text()
        value = json.loads(text)
        quota_project = value.get("quota_project_id")
        return quota_project if isinstance(quota_project, str) and quota_project else None
    except (OSError, ValueError, TypeError):
        return None
