use super::{
    env_bool, env_u64, env_usize, env_var, AsrSettings, BoxError, DEFAULT_AUDIO_FLUSH_LATENCY_MS,
    DEFAULT_AUDIO_MAX_BATCH_MS, DEFAULT_AUDIO_QUEUE_CHUNKS, DEFAULT_CHUNK_MS,
    DEFAULT_FORCE_END_TURN_MS, DEFAULT_MODEL, DEFAULT_PARTIAL_UI_INTERVAL_MS, DEFAULT_SAMPLE_RATE,
    DEFAULT_STT_CONNECT_TIMEOUT_MS, DEFAULT_STT_MAX_RECONNECTS, DEFAULT_STT_RECONNECT_BACKOFF_MS,
    DEFAULT_STT_RECONNECT_MAX_BACKOFF_MS, INWORLD_STT_WS_URL,
};
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::{
    client::IntoClientRequest,
    http::{header::AUTHORIZATION, HeaderValue},
};
use url::Url;

pub(super) struct InworldConfig {
    pub(super) api_key: String,
    pub(super) ws_url: String,
    pub(super) model: String,
    pub(super) language: Option<String>,
    pub(super) sample_rate: u32,
    pub(super) chunk_ms: u32,
    pub(super) enable_language_detection: bool,
    pub(super) enable_speaker_diarization: bool,
    pub(super) mic_device: Option<String>,
    pub(super) show_partials: bool,
    pub(super) partial_ui_interval: Duration,
    pub(super) audio_queue_chunks: usize,
    pub(super) audio_max_batch: Duration,
    pub(super) audio_flush_latency: Option<Duration>,
    pub(super) force_end_turn_after: Option<Duration>,
    pub(super) reconnect: ReconnectConfig,
    pub(super) socks_proxy: Option<SocksProxy>,
}

#[derive(Clone, Copy)]
pub(super) struct ReconnectConfig {
    pub(super) max_reconnects: usize,
    pub(super) reconnect_backoff: Duration,
    pub(super) reconnect_max_backoff: Duration,
    pub(super) connect_timeout: Duration,
}

pub(super) struct SocksProxy {
    pub(super) host: String,
    pub(super) port: u16,
    pub(super) username: Option<String>,
    pub(super) password: Option<String>,
}

impl InworldConfig {
    pub(super) fn from_env(settings: AsrSettings) -> Result<Self, BoxError> {
        let api_key = env_var("INWORLD_API_KEY").ok_or("missing INWORLD_API_KEY")?;
        let chunk_ms = env_var("INWORLD_STT_CHUNK_MS")
            .and_then(|value| value.parse().ok())
            .unwrap_or(DEFAULT_CHUNK_MS);
        let audio_queue_chunks =
            env_usize("INWORLD_AUDIO_QUEUE_CHUNKS", DEFAULT_AUDIO_QUEUE_CHUNKS).max(1);
        let audio_max_batch_ms =
            env_u64("INWORLD_AUDIO_MAX_BATCH_MS", DEFAULT_AUDIO_MAX_BATCH_MS).max(chunk_ms as u64);
        let audio_flush_latency = env_var("INWORLD_AUDIO_FLUSH_LATENCY_MS")
            .and_then(|value| value.parse::<u64>().ok())
            .or(Some(DEFAULT_AUDIO_FLUSH_LATENCY_MS))
            .filter(|value| *value > 0)
            .map(Duration::from_millis);
        let language_override = settings
            .language
            .map(|language| language.code().to_string());
        let enable_language_detection = if language_override.is_none() {
            env_bool("INWORLD_STT_LANGUAGE_DETECTION", true)
        } else {
            false
        };

        Ok(Self {
            api_key,
            ws_url: env_var("INWORLD_STT_WS_URL").unwrap_or_else(|| INWORLD_STT_WS_URL.to_string()),
            model: env_var("INWORLD_STT_MODEL").unwrap_or_else(|| DEFAULT_MODEL.to_string()),
            language: language_override.or_else(|| env_var("INWORLD_STT_LANGUAGE")),
            sample_rate: env_var("INWORLD_STT_SAMPLE_RATE")
                .and_then(|value| value.parse().ok())
                .unwrap_or(DEFAULT_SAMPLE_RATE),
            chunk_ms,
            enable_language_detection,
            enable_speaker_diarization: env_bool("INWORLD_ENABLE_SPEAKER_DIARIZATION", true),
            mic_device: env_var("INWORLD_MIC_DEVICE"),
            show_partials: env_bool(
                "INWORLD_SHOW_PARTIALS",
                env_bool("INWORLD_PRINT_PARTIALS", true),
            ),
            partial_ui_interval: Duration::from_millis(env_u64(
                "INWORLD_PARTIAL_UI_INTERVAL_MS",
                DEFAULT_PARTIAL_UI_INTERVAL_MS,
            )),
            audio_queue_chunks,
            audio_max_batch: Duration::from_millis(audio_max_batch_ms),
            audio_flush_latency,
            force_end_turn_after: env_var("INWORLD_FORCE_END_TURN_MS")
                .and_then(|value| value.parse::<u64>().ok())
                .or(Some(DEFAULT_FORCE_END_TURN_MS))
                .filter(|value| *value > 0)
                .map(Duration::from_millis),
            reconnect: ReconnectConfig::from_env(),
            socks_proxy: SocksProxy::from_env()?,
        })
    }

    pub(super) fn debug_summary(&self) -> String {
        format!(
            "config model={} sample_rate={} chunk_ms={} language={} language_detection={} speaker_diarization={} show_partials={} partial_ui_interval_ms={} force_end_turn_ms={} audio_queue_chunks={} audio_max_batch_ms={} audio_flush_latency_ms={} max_reconnects={} reconnect_backoff_ms={} reconnect_max_backoff_ms={} connect_timeout_ms={} network={}",
            self.model,
            self.sample_rate,
            self.chunk_ms,
            self.language.as_deref().unwrap_or("auto"),
            self.enable_language_detection,
            self.enable_speaker_diarization,
            self.show_partials,
            self.partial_ui_interval.as_millis(),
            self.force_end_turn_after
                .map(|duration| duration.as_millis().to_string())
                .unwrap_or_else(|| "off".to_string()),
            self.audio_queue_chunks,
            self.audio_max_batch.as_millis(),
            self.audio_flush_latency
                .map(|duration| duration.as_millis().to_string())
                .unwrap_or_else(|| "off".to_string()),
            self.reconnect.max_reconnects,
            self.reconnect.reconnect_backoff.as_millis(),
            self.reconnect.reconnect_max_backoff.as_millis(),
            self.reconnect.connect_timeout.as_millis(),
            if self.socks_proxy.is_some() { "socks5" } else { "direct" },
        )
    }

    pub(super) fn request(
        &self,
    ) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request, BoxError> {
        let mut request = self.ws_url.as_str().into_client_request()?;

        request.headers_mut().insert(
            AUTHORIZATION,
            HeaderValue::from_str(&self.authorization_header())?,
        );

        Ok(request)
    }

    pub(super) fn ws_host(&self) -> Result<String, BoxError> {
        Url::parse(&self.ws_url)?
            .host_str()
            .map(str::to_string)
            .ok_or_else(|| "missing Inworld STT host".into())
    }

    pub(super) fn ws_port(&self) -> Result<u16, BoxError> {
        Url::parse(&self.ws_url)?
            .port_or_known_default()
            .ok_or_else(|| "missing Inworld STT port".into())
    }

    fn authorization_header(&self) -> String {
        if self.api_key.to_ascii_lowercase().starts_with("basic ") {
            self.api_key.clone()
        } else {
            format!("Basic {}", self.api_key)
        }
    }

    pub(super) fn transcribe_config(&self) -> Value {
        let mut config = json!({
            "modelId": self.model,
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": self.sample_rate,
            "numberOfChannels": 1,
            "enableSpeakerDiarization": self.enable_speaker_diarization,
        });

        if let Some(language) = &self.language {
            config["language"] = json!(language);
        } else {
            config["enableLanguageDetection"] = json!(self.enable_language_detection);
        }

        json!({ "transcribe_config": config })
    }
}

impl ReconnectConfig {
    pub(super) fn from_env() -> Self {
        Self::from_values(env_var)
    }

    fn from_values(mut value: impl FnMut(&str) -> Option<String>) -> Self {
        let max_reconnects = value("INWORLD_STT_MAX_RECONNECTS")
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(DEFAULT_STT_MAX_RECONNECTS);
        let reconnect_backoff_ms = value("INWORLD_STT_RECONNECT_BACKOFF_MS")
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(DEFAULT_STT_RECONNECT_BACKOFF_MS);
        let reconnect_max_backoff_ms = value("INWORLD_STT_RECONNECT_MAX_BACKOFF_MS")
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(DEFAULT_STT_RECONNECT_MAX_BACKOFF_MS)
            .max(reconnect_backoff_ms);
        let connect_timeout_ms = value("INWORLD_STT_CONNECT_TIMEOUT_MS")
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(DEFAULT_STT_CONNECT_TIMEOUT_MS)
            .max(1);

        Self {
            max_reconnects,
            reconnect_backoff: Duration::from_millis(reconnect_backoff_ms),
            reconnect_max_backoff: Duration::from_millis(reconnect_max_backoff_ms),
            connect_timeout: Duration::from_millis(connect_timeout_ms),
        }
    }
}

impl SocksProxy {
    fn from_env() -> Result<Option<Self>, BoxError> {
        let Some(raw_proxy) = env_var("OUTBOUND_PROXY") else {
            return Ok(None);
        };

        Ok(Some(Self::parse(&raw_proxy)?))
    }

    fn parse(raw_proxy: &str) -> Result<Self, BoxError> {
        let url = Url::parse(raw_proxy)?;
        if !matches!(url.scheme(), "socks5" | "socks5h") {
            return Err(format!("unsupported OUTBOUND_PROXY scheme: {}", url.scheme()).into());
        }

        let host = url
            .host_str()
            .map(str::to_string)
            .ok_or("missing OUTBOUND_PROXY host")?;
        let port = url.port_or_known_default().unwrap_or(1080);
        let username = (!url.username().is_empty()).then(|| url.username().to_string());
        let password = url.password().map(str::to_string);

        Ok(Self {
            host,
            port,
            username,
            password,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::asr::AsrLanguagePreset;

    fn test_config() -> InworldConfig {
        InworldConfig {
            api_key: "abc".to_string(),
            ws_url: INWORLD_STT_WS_URL.to_string(),
            model: "model".to_string(),
            language: None,
            sample_rate: 16_000,
            chunk_ms: 100,
            enable_language_detection: true,
            enable_speaker_diarization: true,
            mic_device: None,
            show_partials: true,
            partial_ui_interval: Duration::from_millis(120),
            audio_queue_chunks: 200,
            audio_max_batch: Duration::from_millis(800),
            audio_flush_latency: Some(Duration::from_millis(10_000)),
            force_end_turn_after: Some(Duration::from_millis(4_000)),
            reconnect: ReconnectConfig {
                max_reconnects: 3,
                reconnect_backoff: Duration::from_millis(750),
                reconnect_max_backoff: Duration::from_millis(5_000),
                connect_timeout: Duration::from_millis(10_000),
            },
            socks_proxy: None,
        }
    }

    #[test]
    fn transcribe_config_uses_language_detection_by_default() {
        let config = test_config();

        assert_eq!(
            config.transcribe_config()["transcribe_config"]["enableLanguageDetection"],
            true
        );
        assert!(config.transcribe_config()["transcribe_config"]
            .get("language")
            .is_none());
    }

    #[test]
    fn language_override_disables_detection_shape() {
        let mut config = test_config();
        config.language = Some(AsrLanguagePreset::Russian.code().to_string());
        config.enable_language_detection = false;

        let value = config.transcribe_config();

        assert_eq!(value["transcribe_config"]["language"], "ru");
        assert!(value["transcribe_config"]
            .get("enableLanguageDetection")
            .is_none());
    }

    #[test]
    fn socks_proxy_parse_supports_auth_and_default_port() {
        let proxy = SocksProxy::parse("socks5h://user:pass@example.com").unwrap();

        assert_eq!(proxy.host, "example.com");
        assert_eq!(proxy.port, 1080);
        assert_eq!(proxy.username.as_deref(), Some("user"));
        assert_eq!(proxy.password.as_deref(), Some("pass"));
    }

    #[test]
    fn reconnect_config_uses_defaults_and_env_overrides() {
        let defaults = ReconnectConfig::from_values(|_| None);
        assert_eq!(defaults.max_reconnects, 3);
        assert_eq!(defaults.reconnect_backoff, Duration::from_millis(750));
        assert_eq!(defaults.reconnect_max_backoff, Duration::from_millis(5_000));
        assert_eq!(defaults.connect_timeout, Duration::from_millis(10_000));

        let overrides = ReconnectConfig::from_values(|name| match name {
            "INWORLD_STT_MAX_RECONNECTS" => Some("5".to_string()),
            "INWORLD_STT_RECONNECT_BACKOFF_MS" => Some("250".to_string()),
            "INWORLD_STT_RECONNECT_MAX_BACKOFF_MS" => Some("1000".to_string()),
            "INWORLD_STT_CONNECT_TIMEOUT_MS" => Some("2000".to_string()),
            _ => None,
        });

        assert_eq!(overrides.max_reconnects, 5);
        assert_eq!(overrides.reconnect_backoff, Duration::from_millis(250));
        assert_eq!(
            overrides.reconnect_max_backoff,
            Duration::from_millis(1_000)
        );
        assert_eq!(overrides.connect_timeout, Duration::from_millis(2_000));
    }

    #[test]
    fn reconnect_config_keeps_max_backoff_at_least_base_and_timeout_nonzero() {
        let config = ReconnectConfig::from_values(|name| match name {
            "INWORLD_STT_RECONNECT_BACKOFF_MS" => Some("1000".to_string()),
            "INWORLD_STT_RECONNECT_MAX_BACKOFF_MS" => Some("100".to_string()),
            "INWORLD_STT_CONNECT_TIMEOUT_MS" => Some("0".to_string()),
            _ => None,
        });

        assert_eq!(config.reconnect_backoff, Duration::from_millis(1_000));
        assert_eq!(config.reconnect_max_backoff, Duration::from_millis(1_000));
        assert_eq!(config.connect_timeout, Duration::from_millis(1));
    }
}
