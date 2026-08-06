from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CEREBRAS_API_BASE = "https://api.cerebras.ai/v1"
DEFAULT_CEREBRAS_MODEL = "zai-glm-4.7"
DEFAULT_CEREBRAS_STAGE_MODEL = DEFAULT_CEREBRAS_MODEL
DEFAULT_HELP_OPENER_PRIMARY_MODEL = "zai-glm-4.7"
DEFAULT_HELP_OPENER_SECONDARY_MODEL = "gpt-oss-120b"
DEFAULT_STUDENT_TRANSLATION_MODEL = "gpt-oss-120b"
DEFAULT_STUDENT_ANSWER_MODEL = "gemini-3.5-flash"
DEFAULT_VERTEX_MODEL = "gemini-3.5-flash"
DEFAULT_VERTEX_STAGE_MODEL = "gemini-3.5-flash"
DEFAULT_VERTEX_SCORECARD_MODEL = DEFAULT_VERTEX_MODEL
DEFAULT_VERTEX_LIVE_MODEL = "gemini-2.0-flash-live-preview-04-09"
DEFAULT_VERTEX_LIVE_ASR_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_VERTEX_LIVE_ASR_LOCATION = "us-central1"
DEFAULT_VERTEX_LIVE_STAGE_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_TIMEOUT_SECS = 30.0
DEFAULT_RATE_LIMIT_BACKOFF_MS = 15_000
DEFAULT_HELP_OPENER_TIMEOUT_MS = 4_000
DEFAULT_REASONING_EFFORT = "none"
DEFAULT_VERTEX_THINKING_LEVEL = "low"
DEFAULT_VERTEX_SCORECARD_THINKING_LEVEL = "minimal"
DEFAULT_INTELLIGENCE_TRANSPORT = "rest"
DEFAULT_VERTEX_LIVE_TIMEOUT_SECS = 20
DEFAULT_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def env_var(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_optional_thinking_level(name: str, default: str | None) -> str | None:
    raw = os.getenv(name)
    if raw is not None and raw.strip().lower() in {"0", "false", "none", "off", "disabled"}:
        return None
    return env_var(name) or default


def env_optional_vertex_thinking_level() -> str | None:
    return env_optional_thinking_level("VERTEX_THINKING_LEVEL", DEFAULT_VERTEX_THINKING_LEVEL)


def env_optional_vertex_scorecard_thinking_level() -> str | None:
    return env_optional_thinking_level(
        "VERTEX_SCORECARD_THINKING_LEVEL",
        DEFAULT_VERTEX_SCORECARD_THINKING_LEVEL,
    )


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


def default_openrouter_model(vertex_model: str) -> str:
    model = vertex_model.strip()
    if "/" in model:
        return model
    return f"google/{model}"


@dataclass(frozen=True)
class Settings:
    provider: str
    service_token: str | None
    outbound_proxy: str | None
    timeout_secs: float
    rate_limit_backoff_ms: int
    help_opener_timeout_ms: int
    intelligence_transport: str

    cerebras_api_key: str | None
    cerebras_api_base: str
    cerebras_model: str
    cerebras_stage_model: str
    help_opener_primary_model: str
    help_opener_secondary_model: str
    student_translation_model: str
    student_answer_model: str
    cerebras_reasoning_effort: str
    cerebras_prompt_cache_key: bool

    vertex_project: str | None
    vertex_location: str
    vertex_model: str
    vertex_stage_model: str
    vertex_scorecard_model: str
    vertex_live_model: str
    vertex_live_timeout_secs: float
    vertex_live_asr_model: str
    vertex_live_asr_location: str
    vertex_live_asr_timeout_secs: float
    vertex_live_stage_model: str
    vertex_live_stage_location: str
    vertex_live_stage_timeout_secs: float
    vertex_api_base: str
    vertex_access_token: str | None
    vertex_adc_credentials_path: str | None
    vertex_quota_project_id: str | None
    vertex_thinking_level: str | None
    vertex_scorecard_thinking_level: str | None

    openrouter_api_key: str | None
    openrouter_api_base: str
    openrouter_gemini_model: str
    openrouter_site_url: str | None
    openrouter_app_name: str | None
    openrouter_proxy: str | None

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
        vertex_live_asr_location = (
            env_var("VERTEX_LIVE_ASR_LOCATION")
            or env_var("GEMINI_LIVE_ASR_LOCATION")
            or (
                vertex_location
                if vertex_location.lower() != "global"
                else DEFAULT_VERTEX_LIVE_ASR_LOCATION
            )
        )

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
            intelligence_transport=(
                env_var("COACH_INTELLIGENCE_TRANSPORT") or DEFAULT_INTELLIGENCE_TRANSPORT
            ).lower(),
            cerebras_api_key=env_var("CEREBRAS_API_KEY"),
            cerebras_api_base=env_var("CEREBRAS_API_BASE") or DEFAULT_CEREBRAS_API_BASE,
            cerebras_model=env_var("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL,
            cerebras_stage_model=env_var("CEREBRAS_STAGE_MODEL")
            or env_var("COACH_STAGE_MODEL")
            or env_var("CEREBRAS_MODEL")
            or DEFAULT_CEREBRAS_STAGE_MODEL,
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
            student_translation_model=(
                env_var("STUDENT_TRANSLATION_MODEL") or DEFAULT_STUDENT_TRANSLATION_MODEL
            ),
            student_answer_model=(
                env_var("STUDENT_ANSWER_MODEL")
                or env_var("VERTEX_GEMINI_MODEL")
                or DEFAULT_STUDENT_ANSWER_MODEL
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
            vertex_stage_model=env_var("VERTEX_STAGE_MODEL")
            or env_var("GEMINI_VERTEX_STAGE_MODEL")
            or DEFAULT_VERTEX_STAGE_MODEL,
            vertex_scorecard_model=env_var("VERTEX_SCORECARD_MODEL")
            or env_var("GEMINI_VERTEX_SCORECARD_MODEL")
            or env_var("VERTEX_GEMINI_MODEL")
            or DEFAULT_VERTEX_SCORECARD_MODEL,
            vertex_live_model=env_var("VERTEX_LIVE_MODEL")
            or env_var("GEMINI_VERTEX_LIVE_MODEL")
            or DEFAULT_VERTEX_LIVE_MODEL,
            vertex_live_timeout_secs=float(
                env_int("VERTEX_LIVE_TIMEOUT_SECS", DEFAULT_VERTEX_LIVE_TIMEOUT_SECS)
            ),
            vertex_live_asr_model=env_var("VERTEX_LIVE_ASR_MODEL")
            or env_var("GEMINI_LIVE_ASR_MODEL")
            or DEFAULT_VERTEX_LIVE_ASR_MODEL,
            vertex_live_asr_location=vertex_live_asr_location,
            vertex_live_asr_timeout_secs=float(
                env_int("VERTEX_LIVE_ASR_TIMEOUT_SECS", DEFAULT_VERTEX_LIVE_TIMEOUT_SECS)
            ),
            vertex_live_stage_model=env_var("VERTEX_LIVE_STAGE_MODEL")
            or env_var("COACH_STAGE_AUDIO_LIVE_MODEL")
            or DEFAULT_VERTEX_LIVE_STAGE_MODEL,
            vertex_live_stage_location=env_var("VERTEX_LIVE_STAGE_LOCATION")
            or env_var("COACH_STAGE_AUDIO_LIVE_LOCATION")
            or vertex_live_asr_location,
            vertex_live_stage_timeout_secs=float(
                env_int(
                    "VERTEX_LIVE_STAGE_TIMEOUT_SECS",
                    env_int("COACH_STAGE_AUDIO_LIVE_TIMEOUT_SECS", DEFAULT_VERTEX_LIVE_TIMEOUT_SECS),
                )
            ),
            vertex_api_base=vertex_api_base,
            vertex_access_token=env_var("VERTEX_ACCESS_TOKEN")
            or env_var("GOOGLE_OAUTH_ACCESS_TOKEN"),
            vertex_adc_credentials_path=adc_path,
            vertex_quota_project_id=env_var("GOOGLE_CLOUD_QUOTA_PROJECT")
            or quota_project_from_adc(adc_path),
            vertex_thinking_level=env_optional_vertex_thinking_level(),
            vertex_scorecard_thinking_level=env_optional_vertex_scorecard_thinking_level(),
            openrouter_api_key=env_var("OPENROUTER_API_KEY"),
            openrouter_api_base=env_var("OPENROUTER_API_BASE") or DEFAULT_OPENROUTER_API_BASE,
            openrouter_gemini_model=(
                env_var("OPENROUTER_GEMINI_MODEL")
                or env_var("OPENROUTER_MODEL")
                or default_openrouter_model(
                    env_var("VERTEX_GEMINI_MODEL")
                    or env_var("GEMINI_VERTEX_MODEL")
                    or DEFAULT_VERTEX_MODEL
                )
            ),
            openrouter_site_url=env_var("OPENROUTER_SITE_URL"),
            openrouter_app_name=env_var("OPENROUTER_APP_NAME") or "rec-sidecar",
            openrouter_proxy=env_var("OPENROUTER_PROXY") or env_var("OUTBOUND_PROXY"),
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

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def gemini_text_configured(self) -> bool:
        return self.openrouter_configured or self.vertex_configured

    @property
    def gemini_text_provider(self) -> str:
        return "openrouter" if self.openrouter_configured else "vertex"

    @property
    def gemini_text_model(self) -> str:
        return self.openrouter_gemini_model if self.openrouter_configured else self.vertex_model

    def provider_label(self) -> str:
        if self.provider == "cerebras":
            return "cerebras" if self.cerebras_configured else "cerebras: disabled"
        if self.provider in {"vertex", "gemini", "google", "openrouter"}:
            return (
                self.gemini_text_provider
                if self.gemini_text_configured
                else "gemini: disabled"
            )
        if self.cerebras_configured and self.gemini_text_configured:
            return f"auto: cerebras -> {self.gemini_text_provider}"
        if self.cerebras_configured:
            return "auto: cerebras"
        if self.gemini_text_configured:
            return f"auto: {self.gemini_text_provider}"
        return "auto: disabled"

    def active_model_label(self) -> str:
        intelligence_suffix = ""
        if self.intelligence_transport in {"live", "websocket", "gemini-live"}:
            intelligence_suffix = f" / intelligence live {self.vertex_live_model}"
        if self.provider in {"vertex", "gemini", "google", "openrouter"}:
            return f"{self.gemini_text_model}{intelligence_suffix}"
        if self.provider == "cerebras":
            return f"{self.cerebras_model}{intelligence_suffix}"
        if self.cerebras_configured and self.gemini_text_configured:
            return (
                f"fast priority {self.gemini_text_model}({self.gemini_text_provider}) "
                f"-> {self.help_opener_primary_model} -> "
                f"{self.help_opener_secondary_model} / slow {self.gemini_text_model} / "
                f"stage {self.cerebras_stage_model} -> "
                f"scorecard {self.gemini_text_model}"
                f"({self.vertex_scorecard_thinking_level or 'default'})"
                f"{intelligence_suffix}"
            )
        if self.gemini_text_configured:
            return f"{self.gemini_text_model}{intelligence_suffix}"
        return f"{self.cerebras_model}{intelligence_suffix}"


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
