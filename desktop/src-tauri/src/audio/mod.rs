mod capture;
mod echo;

use crate::{
    api::ApiClient,
    audio_aec::Aec3Processor,
    models::{
        AudioConfig, AudioConfigPatch, AudioDiagnostics, AudioKind, AudioLaneStatus, AudioSnapshot,
    },
};
use base64::{engine::general_purpose::STANDARD, Engine};
use capture::{AudioChunk, CaptureResource};
use echo::{pcm_rms, EchoClassifier};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex, RwLock,
    },
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{client::IntoClientRequest, http::HeaderValue, Message},
};
use tokio_util::sync::CancellationToken;

const AUDIO_QUEUE: usize = 48;
const VOICE_RMS: f64 = 0.003;
const END_TURN_SILENCE: Duration = Duration::from_millis(650);
const END_TURN_GUARD: Duration = Duration::from_millis(700);

struct LaneHandle {
    cancel: CancellationToken,
    stop: Arc<AtomicBool>,
    _resource: CaptureResource,
}

#[derive(Clone)]
struct AudioShared {
    snapshot: Arc<Mutex<AudioSnapshot>>,
    config: Arc<RwLock<AudioConfig>>,
    aec: Arc<Mutex<Aec3Processor>>,
    echo: Arc<Mutex<EchoClassifier>>,
}

impl AudioShared {
    fn snapshot(&self) -> AudioSnapshot {
        self.snapshot.lock().expect("audio snapshot").clone()
    }

    fn emit(&self, app: &AppHandle) {
        let snapshot = self.snapshot();
        let _ = app.emit("audio://status", &snapshot);
        let _ = app.emit("audio://diagnostics", &snapshot.diagnostics);
    }

    fn set_lane(&self, kind: AudioKind, update: impl FnOnce(&mut AudioLaneStatus)) {
        let mut snapshot = self.snapshot.lock().expect("audio snapshot");
        let lane = match kind {
            AudioKind::System => &mut snapshot.system,
            AudioKind::Microphone => &mut snapshot.microphone,
            AudioKind::All => return,
        };
        update(lane);
    }
}

pub struct AudioManager {
    shared: AudioShared,
    system: Option<LaneHandle>,
    microphone: Option<LaneHandle>,
}

impl AudioManager {
    pub fn new() -> Result<Self, String> {
        let config = AudioConfig::default();
        Ok(Self {
            shared: AudioShared {
                snapshot: Arc::new(Mutex::new(AudioSnapshot {
                    config: config.clone(),
                    ..AudioSnapshot::default()
                })),
                config: Arc::new(RwLock::new(config)),
                aec: Arc::new(Mutex::new(
                    Aec3Processor::new(16_000).map_err(|e| e.to_string())?,
                )),
                echo: Arc::new(Mutex::new(EchoClassifier::default())),
            },
            system: None,
            microphone: None,
        })
    }

    pub fn snapshot(&self) -> AudioSnapshot {
        self.shared.snapshot()
    }

    pub fn configure(&mut self, patch: AudioConfigPatch, app: &AppHandle) -> AudioSnapshot {
        let mut config = self.shared.config.write().expect("audio config");
        if let Some(enabled) = patch.echo_filter {
            config.echo_filter = enabled;
        }
        if let Some(enabled) = patch.aec3 {
            if enabled != config.aec3 {
                if let Ok(mut aec) = self.shared.aec.lock() {
                    aec.reset();
                }
            }
            config.aec3 = enabled;
        }
        if let Some(speaker) = patch.seller_speaker {
            config.seller_speaker = speaker;
        }
        self.shared.snapshot.lock().expect("audio snapshot").config = config.clone();
        tracing::info!(
            echo_filter = config.echo_filter,
            aec3 = config.aec3,
            speaker_mapping = !config.seller_speaker.is_empty(),
            "audio configuration updated"
        );
        drop(config);
        self.shared.emit(app);
        self.snapshot()
    }

    pub fn start(
        &mut self,
        kind: AudioKind,
        session_id: &str,
        token: &str,
        api: ApiClient,
        app: AppHandle,
    ) -> Result<AudioSnapshot, String> {
        match kind {
            AudioKind::System => self.start_lane(AudioKind::System, session_id, token, api, app)?,
            AudioKind::Microphone => {
                self.start_lane(AudioKind::Microphone, session_id, token, api, app)?
            }
            AudioKind::All => {
                self.start_lane(
                    AudioKind::Microphone,
                    session_id,
                    token,
                    api.clone(),
                    app.clone(),
                )?;
                if let Err(error) = self.start_lane(AudioKind::System, session_id, token, api, app)
                {
                    self.stop(AudioKind::Microphone, None);
                    return Err(error);
                }
            }
        }
        Ok(self.snapshot())
    }

    fn start_lane(
        &mut self,
        kind: AudioKind,
        session_id: &str,
        token: &str,
        api: ApiClient,
        app: AppHandle,
    ) -> Result<(), String> {
        let target = match kind {
            AudioKind::System => &mut self.system,
            AudioKind::Microphone => &mut self.microphone,
            AudioKind::All => return Err("invalid audio lane".to_string()),
        };
        if target.is_some() {
            return Ok(());
        }

        let (audio_tx, audio_rx) = mpsc::channel(AUDIO_QUEUE);
        let dropped = Arc::new(AtomicU64::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let mut resource = match kind {
            AudioKind::System => {
                capture::system_audio(audio_tx, Arc::clone(&dropped), Arc::clone(&stop))
            }
            AudioKind::Microphone => {
                capture::microphone(audio_tx, Arc::clone(&dropped), Arc::clone(&stop))
            }
            AudioKind::All => unreachable!(),
        }
        .map_err(|error| {
            tracing::error!(?kind, %session_id, %error, "audio capture initialization failed");
            error
        })?;
        resource.start().map_err(|error| {
            tracing::error!(?kind, %session_id, %error, "audio capture start failed");
            error
        })?;
        tracing::info!(?kind, %session_id, "audio capture started");

        let cancel = CancellationToken::new();
        self.shared.set_lane(kind, |lane| {
            lane.active = true;
            lane.state = "connecting".to_string();
            lane.detail = if kind == AudioKind::System {
                "подключаю системный звук"
            } else {
                "подключаю микрофон"
            }
            .to_string();
        });
        self.shared.emit(&app);

        let task_cancel = cancel.clone();
        let shared = self.shared.clone();
        let lane_session = session_id.to_string();
        let lane_token = token.to_string();
        tauri::async_runtime::spawn(async move {
            run_lane(
                kind,
                lane_session,
                lane_token,
                api,
                audio_rx,
                dropped,
                task_cancel,
                shared,
                app,
            )
            .await;
        });

        *target = Some(LaneHandle {
            cancel,
            stop,
            _resource: resource,
        });
        Ok(())
    }

    pub fn stop(&mut self, kind: AudioKind, app: Option<&AppHandle>) -> AudioSnapshot {
        tracing::info!(?kind, "audio capture stop requested");
        match kind {
            AudioKind::System => stop_handle(&mut self.system),
            AudioKind::Microphone => stop_handle(&mut self.microphone),
            AudioKind::All => {
                stop_handle(&mut self.system);
                stop_handle(&mut self.microphone);
            }
        }
        let mut snapshot = self.shared.snapshot.lock().expect("audio snapshot");
        if matches!(kind, AudioKind::System | AudioKind::All) {
            snapshot.system.active = false;
            snapshot.system.state = "waiting".to_string();
            snapshot.system.detail = "захват остановлен".to_string();
        }
        if matches!(kind, AudioKind::Microphone | AudioKind::All) {
            snapshot.microphone.active = false;
            snapshot.microphone.state = "waiting".to_string();
            snapshot.microphone.detail = "микрофон остановлен".to_string();
        }
        drop(snapshot);
        if let Some(app) = app {
            self.shared.emit(app);
        }
        self.snapshot()
    }
}

fn stop_handle(handle: &mut Option<LaneHandle>) {
    if let Some(handle) = handle.take() {
        handle.stop.store(true, Ordering::Relaxed);
        handle.cancel.cancel();
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_lane(
    kind: AudioKind,
    session_id: String,
    token: String,
    api: ApiClient,
    mut audio_rx: mpsc::Receiver<AudioChunk>,
    dropped: Arc<AtomicU64>,
    cancel: CancellationToken,
    shared: AudioShared,
    app: AppHandle,
) {
    let mut attempt = 0_u32;
    while !cancel.is_cancelled() {
        shared.set_lane(kind, |lane| {
            lane.state = "connecting".to_string();
            lane.detail = if attempt == 0 {
                "подключаю STT".to_string()
            } else {
                format!("переподключаю STT · попытка {attempt}")
            };
        });
        shared.emit(&app);
        match connect_lane(&api, kind, &session_id, &token).await {
            Ok(socket) => {
                attempt = 0;
                tracing::info!(?kind, %session_id, "STT websocket connected");
                shared.set_lane(kind, |lane| {
                    lane.state = "on".to_string();
                    lane.detail = if kind == AudioKind::System {
                        "системный звук стримится"
                    } else {
                        "микрофон стримится"
                    }
                    .to_string();
                });
                shared.emit(&app);
                match stream_lane(
                    kind,
                    socket,
                    &mut audio_rx,
                    &cancel,
                    &shared,
                    &app,
                    &dropped,
                )
                .await
                {
                    Ok(()) => break,
                    Err(error) => tracing::warn!(
                        ?kind,
                        %session_id,
                        %error,
                        "STT websocket disconnected"
                    ),
                }
            }
            Err(error) => {
                tracing::warn!(?kind, %session_id, %error, "STT websocket connection failed");
                shared.set_lane(kind, |lane| {
                    lane.state = "error".to_string();
                    lane.detail = error;
                });
                shared.emit(&app);
            }
        }
        attempt = attempt.saturating_add(1);
        let delay_ms = (500.0 * 1.6_f64.powi(attempt.saturating_sub(1) as i32)).min(5_000.0);
        tokio::select! {
            _ = cancel.cancelled() => break,
            _ = tokio::time::sleep(Duration::from_millis(delay_ms as u64)) => {}
        }
    }
}

type SttSocket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn connect_lane(
    api: &ApiClient,
    kind: AudioKind,
    session_id: &str,
    token: &str,
) -> Result<SttSocket, String> {
    let (role, source) = match kind {
        AudioKind::System => ("client", "remote_audio"),
        AudioKind::Microphone => ("seller", "seller_mic"),
        AudioKind::All => return Err("invalid lane".to_string()),
    };
    let mut url = api
        .websocket_url(&format!("/v1/sessions/{session_id}/stt/live"))
        .map_err(|error| error.to_string())?;
    url.query_pairs_mut()
        .append_pair("role", role)
        .append_pair("source", source);
    let mut request = url
        .as_str()
        .into_client_request()
        .map_err(|error| error.to_string())?;
    request.headers_mut().insert(
        "Authorization",
        HeaderValue::from_str(&format!("Bearer {token}")).map_err(|error| error.to_string())?,
    );
    let (socket, _) = connect_async(request)
        .await
        .map_err(|error| error.to_string())?;
    Ok(socket)
}

async fn stream_lane(
    kind: AudioKind,
    socket: SttSocket,
    audio_rx: &mut mpsc::Receiver<AudioChunk>,
    cancel: &CancellationToken,
    shared: &AudioShared,
    app: &AppHandle,
    dropped: &AtomicU64,
) -> Result<(), String> {
    let (mut outgoing, mut incoming) = socket.split();
    let mut vad = VadState::default();
    let mut frame_count = 0_u64;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                let _ = outgoing.send(Message::Text(json!({"close_stream": {}}).to_string().into())).await;
                return Ok(());
            }
            incoming_message = incoming.next() => {
                match incoming_message {
                    Some(Ok(Message::Text(text))) => handle_server_message(kind, &text, shared, app),
                    Some(Ok(Message::Close(_))) | None => return Err("STT stream closed".to_string()),
                    Some(Err(error)) => return Err(error.to_string()),
                    _ => {}
                }
            }
            chunk = audio_rx.recv() => {
                let Some(chunk) = chunk else { return Ok(()); };
                let Some(processed) = process_chunk(kind, chunk, shared, app) else { continue; };
                let now = Instant::now();
                let rms = pcm_rms(&processed);
                if rms >= VOICE_RMS {
                    vad.speech_open = true;
                    vad.last_voice = Some(now);
                }
                if should_end_turn(&vad, now, rms) {
                    outgoing.send(Message::Text(json!({"end_turn": {}}).to_string().into())).await.map_err(|error| error.to_string())?;
                    vad.last_end_turn = Some(now);
                    vad.speech_open = false;
                    shared.set_lane(kind, |lane| lane.detail = "жду финал".to_string());
                    shared.emit(app);
                    continue;
                }
                if rms < VOICE_RMS && !vad.speech_open {
                    continue;
                }
                outgoing.send(Message::Text(json!({
                    "audio_chunk": {"content": encode_pcm16(&processed)}
                }).to_string().into())).await.map_err(|error| error.to_string())?;
                frame_count += 1;
                shared.set_lane(kind, |lane| {
                    lane.sent_frames = frame_count;
                    lane.dropped_frames = dropped.load(Ordering::Relaxed);
                    if frame_count % 12 == 0 {
                        lane.detail = "слышу речь".to_string();
                    }
                });
                if frame_count % 25 == 0 {
                    shared.emit(app);
                }
            }
        }
    }
}

#[derive(Default)]
struct VadState {
    speech_open: bool,
    last_voice: Option<Instant>,
    last_end_turn: Option<Instant>,
}

fn should_end_turn(vad: &VadState, now: Instant, rms: f64) -> bool {
    vad.speech_open
        && rms < VOICE_RMS
        && vad
            .last_voice
            .is_some_and(|last| now.duration_since(last) >= END_TURN_SILENCE)
        && vad
            .last_end_turn
            .map_or(true, |last| now.duration_since(last) >= END_TURN_GUARD)
}

fn process_chunk(
    kind: AudioKind,
    chunk: AudioChunk,
    shared: &AudioShared,
    app: &AppHandle,
) -> Option<AudioChunk> {
    let now = Instant::now();
    let config = shared.config.read().expect("audio config").clone();
    if kind == AudioKind::System {
        shared
            .echo
            .lock()
            .expect("echo classifier")
            .remember_system(&chunk, now);
        if config.aec3 {
            if let Ok(mut aec) = shared.aec.lock() {
                let _ = aec.process_render_pcm16(&chunk);
                let stats = aec.stats();
                let mut snapshot = shared.snapshot.lock().expect("audio snapshot");
                snapshot.diagnostics.aec3_render_frames = stats.render_frames;
            }
        }
        return Some(chunk);
    }

    if config.aec3 {
        if let Ok(mut aec) = shared.aec.lock() {
            match aec.process_capture_pcm16(&chunk) {
                Ok(clean) if !clean.is_empty() => {
                    let stats = aec.stats();
                    let mut snapshot = shared.snapshot.lock().expect("audio snapshot");
                    snapshot.diagnostics.aec3_capture_frames = stats.capture_frames;
                    return Some(clean);
                }
                Ok(_) => return None,
                Err(error) => {
                    shared.set_lane(kind, |lane| lane.detail = format!("AEC3: {error}"));
                    shared.emit(app);
                }
            }
        }
    }

    if config.echo_filter && pcm_rms(&chunk) >= VOICE_RMS {
        let decision = shared
            .echo
            .lock()
            .expect("echo classifier")
            .classify(&chunk, now);
        let mut snapshot = shared.snapshot.lock().expect("audio snapshot");
        let diagnostics: &mut AudioDiagnostics = &mut snapshot.diagnostics;
        diagnostics.best_correlation = decision.correlation;
        diagnostics.residual_ratio = decision.residual_ratio;
        diagnostics.lag_ms = decision.lag_ms;
        if decision.double_talk {
            diagnostics.double_talk_frames += 1;
        }
        if decision.echo_only {
            diagnostics.suppressed_frames += 1;
            drop(snapshot);
            shared.set_lane(kind, |lane| {
                lane.detail = "эхо клиента подавлено".to_string()
            });
            return None;
        }
    }
    Some(chunk)
}

fn handle_server_message(kind: AudioKind, text: &str, shared: &AudioShared, app: &AppHandle) {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return;
    };
    let event_type = value
        .get("type")
        .and_then(|value| value.as_str())
        .unwrap_or("");
    match event_type {
        "stt.final" => {
            let role = value
                .get("role")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let source = value
                .get("source")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            shared
                .snapshot
                .lock()
                .expect("audio snapshot")
                .diagnostics
                .last_route = format!("{source} → {role}");
            shared.set_lane(kind, |lane| lane.detail = "распознано".to_string());
        }
        "stt.rejected" => {
            let reason = value
                .get("reason")
                .and_then(|value| value.as_str())
                .unwrap_or("подавлено");
            shared.set_lane(kind, |lane| lane.detail = format!("подавлено · {reason}"));
        }
        "error" => {
            let error = value
                .get("error")
                .and_then(|value| value.as_str())
                .unwrap_or("STT error");
            shared.set_lane(kind, |lane| {
                lane.state = "error".to_string();
                lane.detail = error.to_string();
            });
        }
        _ => return,
    }
    shared.emit(app);
}

fn encode_pcm16(samples: &[i16]) -> String {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    STANDARD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;
    use tokio_tungstenite::accept_hdr_async;

    #[test]
    fn pcm16_encoding_is_little_endian() {
        assert_eq!(encode_pcm16(&[1, -2]), "AQD+/w==");
    }

    #[test]
    fn audio_configuration_defaults_to_safe_browser_parity() {
        let manager = AudioManager::new().unwrap();
        let snapshot = manager.snapshot();
        assert!(snapshot.config.echo_filter);
        assert!(!snapshot.config.aec3);
        assert!(!snapshot.system.active);
        assert!(!snapshot.microphone.active);
    }

    #[test]
    fn vad_ends_turn_after_silence_and_respects_guard() {
        let now = Instant::now();
        let mut vad = VadState {
            speech_open: true,
            last_voice: Some(now - Duration::from_millis(651)),
            last_end_turn: None,
        };
        assert!(should_end_turn(&vad, now, 0.0));
        vad.last_end_turn = Some(now - Duration::from_millis(699));
        assert!(!should_end_turn(&vad, now, 0.0));
        vad.last_end_turn = Some(now - Duration::from_millis(700));
        assert!(should_end_turn(&vad, now, 0.0));
        assert!(!should_end_turn(&vad, now, VOICE_RMS));
    }

    #[tokio::test]
    #[allow(clippy::result_large_err)]
    async fn websocket_handshake_carries_bearer_and_audio_route() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (request_tx, request_rx) = oneshot::channel();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut request_tx = Some(request_tx);
            let mut socket = accept_hdr_async(
                stream,
                move |request: &tokio_tungstenite::tungstenite::http::Request<()>, response| {
                    let route = request.uri().to_string();
                    let authorization = request
                        .headers()
                        .get("authorization")
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or("")
                        .to_string();
                    request_tx
                        .take()
                        .unwrap()
                        .send((route, authorization))
                        .unwrap();
                    Ok(response)
                },
            )
            .await
            .unwrap();
            let _ = socket.close(None).await;
        });

        let api = ApiClient::new(&format!("http://{address}")).unwrap();
        let mut socket = connect_lane(&api, AudioKind::System, "sess-ws", "token-ws")
            .await
            .unwrap();
        let (route, authorization) = request_rx.await.unwrap();
        assert_eq!(
            route,
            "/v1/sessions/sess-ws/stt/live?role=client&source=remote_audio"
        );
        assert_eq!(authorization, "Bearer token-ws");
        let _ = socket.close(None).await;
        server.await.unwrap();
    }
}
