use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthUser {
    pub id: String,
    pub email: String,
    #[serde(default = "default_sales_role")]
    pub role: String,
}

fn default_sales_role() -> String {
    "sales".to_string()
}

fn null_to_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthResponse {
    pub user: AuthUser,
    pub token: String,
    #[serde(default)]
    pub expires_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MeResponse {
    pub user: AuthUser,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AssistState {
    #[serde(default)]
    pub fast_text: String,
    #[serde(default)]
    pub slow_text: String,
    #[serde(default)]
    pub streaming: bool,
    #[serde(default)]
    pub generation_id: String,
    #[serde(default)]
    pub fast_model: String,
    #[serde(default)]
    pub slow_model: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StageData {
    #[serde(default)]
    pub stage: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub agenda: String,
    #[serde(default)]
    pub emotion: String,
    #[serde(default)]
    pub step: String,
    #[serde(default)]
    pub provider: String,
    #[serde(default)]
    pub model: String,
    pub confidence: Option<f64>,
    #[serde(default)]
    pub scorecard: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ScorecardData {
    #[serde(default)]
    pub readiness: String,
    #[serde(default)]
    pub readiness_label: String,
    #[serde(default)]
    pub ready_to_advance: bool,
    #[serde(default)]
    pub next_action: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub raw: Option<Value>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SessionState {
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub updated_at: String,
    #[serde(default, deserialize_with = "null_to_default")]
    pub messages: Vec<Value>,
    #[serde(default, deserialize_with = "null_to_default")]
    pub transcript: Vec<Value>,
    #[serde(default)]
    pub client_partial: String,
    #[serde(default)]
    pub seller_draft: String,
    #[serde(default)]
    pub seller_streaming: bool,
    #[serde(default)]
    pub seller_generation_id: String,
    #[serde(default)]
    pub seller_draft_immediate: String,
    #[serde(default)]
    pub seller_immediate_streaming: bool,
    #[serde(default)]
    pub seller_immediate_generation_id: String,
    #[serde(default, deserialize_with = "null_to_default")]
    pub assist: AssistState,
    #[serde(default)]
    pub stage_candidate: Option<StageData>,
    #[serde(default)]
    pub stage_committed: Option<StageData>,
    #[serde(default)]
    pub scorecard: Option<ScorecardData>,
    #[serde(default)]
    pub last_error: String,
    #[serde(default, deserialize_with = "null_to_default")]
    pub events: Vec<Value>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionEnvelope {
    pub session_id: String,
    pub state: SessionState,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionEventRequest {
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub trigger: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub speaker: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConnectionStatus {
    pub state: String,
    pub detail: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AudioKind {
    System,
    Microphone,
    All,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AudioConfig {
    pub echo_filter: bool,
    pub aec3: bool,
    pub seller_speaker: String,
}

impl Default for AudioConfig {
    fn default() -> Self {
        Self {
            echo_filter: true,
            aec3: false,
            seller_speaker: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AudioLaneStatus {
    pub active: bool,
    pub state: String,
    pub detail: String,
    pub sent_frames: u64,
    pub dropped_frames: u64,
}

impl Default for AudioLaneStatus {
    fn default() -> Self {
        Self {
            active: false,
            state: "waiting".to_string(),
            detail: String::new(),
            sent_frames: 0,
            dropped_frames: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct AudioDiagnostics {
    pub suppressed_frames: u64,
    pub double_talk_frames: u64,
    pub best_correlation: f64,
    pub residual_ratio: f64,
    pub lag_ms: u64,
    pub aec3_render_frames: u64,
    pub aec3_capture_frames: u64,
    pub last_route: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct AudioSnapshot {
    pub system: AudioLaneStatus,
    pub microphone: AudioLaneStatus,
    pub config: AudioConfig,
    pub diagnostics: AudioDiagnostics,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AudioConfigPatch {
    pub echo_filter: Option<bool>,
    pub aec3: Option<bool>,
    pub seller_speaker: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn current_gateway_session_shape_deserializes_and_keeps_unknown_fields() {
        let raw = r#"{
          "session_id":"sess-1",
          "seller_draft":"Следующая реплика",
          "seller_streaming":true,
          "assist":{"fast_text":"Скажите сейчас","streaming":true},
          "stage_committed":{"stage":"discovery","title":"Диагностика"},
          "scorecard":{"readiness":"yellow","next_action":"Спросить","signals":[{"key":"need"}]},
          "events":[],
          "future_field":{"ok":true}
        }"#;
        let state: SessionState = serde_json::from_str(raw).unwrap();
        assert_eq!(state.session_id, "sess-1");
        assert!(state.seller_streaming);
        assert_eq!(state.assist.fast_text, "Скажите сейчас");
        assert!(state.extra.contains_key("future_field"));
        assert!(state
            .scorecard
            .as_ref()
            .unwrap()
            .extra
            .contains_key("signals"));

        let serialized = serde_json::to_value(state).unwrap();
        assert_eq!(serialized["future_field"]["ok"], true);
    }

    #[test]
    fn production_null_collections_deserialize_as_empty() {
        let raw = r#"{
          "session_id":"sess-nullable",
          "messages":null,
          "transcript":null,
          "assist":null,
          "events":null
        }"#;
        let state: SessionState = serde_json::from_str(raw).unwrap();
        assert!(state.messages.is_empty());
        assert!(state.transcript.is_empty());
        assert!(state.assist.fast_text.is_empty());
        assert!(!state.assist.streaming);
        assert!(state.events.is_empty());
    }
}
