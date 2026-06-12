use base64::{engine::general_purpose, Engine as _};
use chrono::Local;
use cpal::{
    traits::{DeviceTrait, HostTrait, StreamTrait},
    SampleFormat, Stream, StreamConfig,
};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    env,
    error::Error,
    fmt,
    fs::{create_dir_all, OpenOptions},
    io::Write,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self as std_mpsc, Receiver as StdReceiver, Sender},
        Arc, Mutex, OnceLock,
    },
    thread,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpStream,
    sync::mpsc,
};
use tokio_socks::tcp::Socks5Stream;
use tokio_tungstenite::{client_async_tls, connect_async, tungstenite::Message, WebSocketStream};

mod audio;
mod config;

use audio::{build_audio_batch, chunks_for_duration, AudioProcessor};
use config::{InworldConfig, ReconnectConfig, SocksProxy};

const INWORLD_STT_WS_URL: &str = "wss://api.inworld.ai/stt/v1/transcribe:streamBidirectional";
const DEFAULT_MODEL: &str = "soniox/stt-rt-v4";
const DEFAULT_SAMPLE_RATE: u32 = 16_000;
const DEFAULT_CHUNK_MS: u32 = 100;
const DEFAULT_FORCE_END_TURN_MS: u64 = 4_000;
const DEFAULT_PARTIAL_UI_INTERVAL_MS: u64 = 120;
const DEFAULT_AUDIO_QUEUE_CHUNKS: usize = 200;
const DEFAULT_AUDIO_MAX_BATCH_MS: u64 = 800;
const DEFAULT_AUDIO_FLUSH_LATENCY_MS: u64 = 10_000;
const DEFAULT_STT_MAX_RECONNECTS: usize = 3;
const DEFAULT_STT_RECONNECT_BACKOFF_MS: u64 = 750;
const DEFAULT_STT_RECONNECT_MAX_BACKOFF_MS: u64 = 5_000;
const DEFAULT_STT_CONNECT_TIMEOUT_MS: u64 = 10_000;
const DEFAULT_LOG_PATH: &str = "logs/rec-sidecar.log";
const DEFAULT_RAW_LOG_PATH: &str = "logs/rec-sidecar.raw.log";

static LOG_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

type BoxError = Box<dyn Error + Send + Sync>;

pub enum AsrEvent {
    Connecting(String),
    Recovering(String),
    Ready(String),
    PartialTranscript(String),
    Transcript(String),
    Error(String),
    Stopped,
}

pub enum AsrCommand {
    Flush { reason: String },
}

type SharedCommandRx = Arc<Mutex<StdReceiver<AsrCommand>>>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AsrFailureKind {
    Recoverable,
    Terminal,
}

#[derive(Debug)]
struct AsrFailure {
    kind: AsrFailureKind,
    message: String,
}

impl AsrFailure {
    fn new(kind: AsrFailureKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}

impl fmt::Display for AsrFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl Error for AsrFailure {}

#[derive(Clone, Default)]
pub struct AsrSettings {
    pub language: Option<AsrLanguagePreset>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum AsrLanguagePreset {
    Russian,
    English,
}

impl AsrLanguagePreset {
    fn code(self) -> &'static str {
        match self {
            Self::Russian => "ru",
            Self::English => "en",
        }
    }
}

pub fn spawn_asr_worker(
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    settings: AsrSettings,
) -> Option<Sender<AsrCommand>> {
    let (command_tx, command_rx) = std_mpsc::channel();

    if env_var("INWORLD_API_KEY").is_some() {
        spawn_inworld_asr(tx, stop, settings, command_rx);
    } else {
        spawn_mock_asr(tx, stop, command_rx);
    }

    Some(command_tx)
}

fn env_var(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn env_bool(name: &str, default: bool) -> bool {
    env_var(name)
        .map(|value| {
            matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(default)
}

fn env_u64(name: &str, default: u64) -> u64 {
    env_var(name)
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn env_usize(name: &str, default: usize) -> usize {
    env_var(name)
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn spawn_inworld_asr(
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    settings: AsrSettings,
    command_rx: StdReceiver<AsrCommand>,
) {
    thread::spawn(move || {
        debug_log("session start");
        let _ = tx.send(AsrEvent::Connecting(
            "Connecting to Inworld Soniox STT...".to_string(),
        ));

        let runtime = match tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
        {
            Ok(runtime) => runtime,
            Err(err) => {
                debug_log(format!("runtime error: {}", err));
                let _ = tx.send(AsrEvent::Error(format!(
                    "Inworld STT runtime error:\n{}",
                    err
                )));
                return;
            }
        };

        if let Err(err) = runtime.block_on(run_inworld_asr(tx.clone(), stop, settings, command_rx))
        {
            debug_log(format!("session error: {}", err));
            let _ = tx.send(AsrEvent::Error(format!("Inworld STT error:\n{}", err)));
        }
    });
}

async fn run_inworld_asr(
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    settings: AsrSettings,
    command_rx: StdReceiver<AsrCommand>,
) -> Result<(), BoxError> {
    let config = InworldConfig::from_env(settings)?;
    debug_log(config.debug_summary());
    let command_rx = Arc::new(Mutex::new(command_rx));
    let mut reconnect_attempt = 0_usize;

    loop {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }

        match run_inworld_session_once(tx.clone(), stop.clone(), &config, command_rx.clone()).await
        {
            Ok(()) => return Ok(()),
            Err(err) if stop.load(Ordering::Relaxed) => {
                debug_log(format!("session ended after stop: {}", err));
                return Ok(());
            }
            Err(err)
                if is_recoverable_asr_error(&err)
                    && reconnect_attempt < config.reconnect.max_reconnects =>
            {
                reconnect_attempt += 1;
                let delay = reconnect_backoff(&config.reconnect, reconnect_attempt);
                let message = recovering_status(reconnect_attempt, &config.reconnect, delay);
                debug_log(format!(
                    "session recoverable error attempt={}/{} delay_ms={} error={}",
                    reconnect_attempt,
                    config.reconnect.max_reconnects,
                    delay.as_millis(),
                    err
                ));
                let _ = tx.send(AsrEvent::Recovering(message));
                wait_reconnect_delay(&stop, delay).await;
            }
            Err(err) => return Err(err),
        }
    }
}

async fn run_inworld_session_once(
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    config: &InworldConfig,
    command_rx: SharedCommandRx,
) -> Result<(), BoxError> {
    let request = config.request().map_err(|err| {
        terminal_asr_error(format!("invalid Inworld STT request/auth config: {}", err))
    })?;

    if let Some(proxy) = &config.socks_proxy {
        debug_log(format!(
            "connecting socks5 proxy {}:{}",
            proxy.host, proxy.port
        ));
        let _ = tx.send(AsrEvent::Connecting(
            "Connecting through SOCKS5 proxy...".to_string(),
        ));
        let socks = match tokio::time::timeout(
            config.reconnect.connect_timeout,
            connect_socks5(proxy, config),
        )
        .await
        {
            Ok(result) => result?,
            Err(_) => {
                return Err(recoverable_asr_error(format!(
                    "SOCKS5 connection timed out after {} ms",
                    config.reconnect.connect_timeout.as_millis()
                )));
            }
        };
        debug_log("socks5 connected");
        let _ = tx.send(AsrEvent::Connecting(
            "Opening Inworld WebSocket...".to_string(),
        ));
        let (ws, _) = match tokio::time::timeout(
            config.reconnect.connect_timeout,
            client_async_tls(request, socks),
        )
        .await
        {
            Ok(result) => result?,
            Err(_) => {
                return Err(recoverable_asr_error(format!(
                    "Inworld STT websocket timed out after {} ms",
                    config.reconnect.connect_timeout.as_millis()
                )));
            }
        };
        debug_log("websocket connected via socks5");
        run_websocket(ws, config, tx, stop, command_rx).await
    } else {
        let _ = tx.send(AsrEvent::Connecting(
            "Opening Inworld WebSocket...".to_string(),
        ));
        let (ws, _) =
            match tokio::time::timeout(config.reconnect.connect_timeout, connect_async(request))
                .await
            {
                Ok(result) => result?,
                Err(_) => {
                    return Err(recoverable_asr_error(format!(
                        "Inworld STT websocket timed out after {} ms",
                        config.reconnect.connect_timeout.as_millis()
                    )));
                }
            };
        debug_log("websocket connected direct");
        run_websocket(ws, config, tx, stop, command_rx).await
    }
}

async fn run_websocket<S>(
    ws: WebSocketStream<S>,
    config: &InworldConfig,
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    command_rx: SharedCommandRx,
) -> Result<(), BoxError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (audio_tx, mut audio_rx) = mpsc::channel(config.audio_queue_chunks);
    let (mut write, mut read) = ws.split();
    let timing = Arc::new(Mutex::new(AsrTiming::default()));
    let dropped_audio_chunks = Arc::new(AtomicU64::new(0));
    let raw_log = env_bool("REC_SIDECAR_LOG_RAW", false);

    debug_log(format!(
        "send transcribe_config {}",
        config.transcribe_config()
    ));
    write
        .send(Message::Text(config.transcribe_config().to_string().into()))
        .await?;

    let _ = tx.send(AsrEvent::Connecting("Starting microphone...".to_string()));
    let stream = start_audio_stream(
        config,
        audio_tx,
        dropped_audio_chunks.clone(),
        stop.clone(),
        tx.clone(),
    )
    .map_err(|err| terminal_asr_error(format!("microphone setup failed: {}", err)))?;
    stream
        .play()
        .map_err(|err| terminal_asr_error(format!("microphone start failed: {}", err)))?;

    let _ = tx.send(AsrEvent::Ready(format!(
        "Inworld Soniox STT connected\nmodel: {}\nnetwork: {}\naudio: native Rust mic -> LINEAR16 {} Hz",
        config.model,
        if config.socks_proxy.is_some() {
            "SOCKS5 proxy"
        } else {
            "direct"
        },
        config.sample_rate
    )));
    debug_log("asr ready");

    let sender_stop = stop.clone();
    let force_end_turn_after = config.force_end_turn_after;
    let sender_timing = timing.clone();
    let sender_dropped_audio_chunks = dropped_audio_chunks.clone();
    let max_batch_chunks =
        chunks_for_duration(config.audio_max_batch, config.chunk_ms).min(config.audio_queue_chunks);
    let flush_latency_chunks = config
        .audio_flush_latency
        .map(|duration| chunks_for_duration(duration, config.chunk_ms));
    let mut sender = tokio::spawn(async move {
        let mut last_forced_end_turn = Instant::now();
        let mut sent_audio_since_end_turn = false;
        let mut total_chunks = 0_u64;
        let mut total_messages = 0_u64;
        let mut chunks_since_log = 0_u64;
        let mut messages_since_log = 0_u64;
        let mut bytes_since_log = 0_usize;
        let mut flushed_since_log = 0_u64;
        let mut last_audio_log = Instant::now();

        while !sender_stop.load(Ordering::Relaxed) {
            for reason in drain_asr_commands(&command_rx) {
                send_end_turn(&mut write, &reason, &sender_timing).await?;
            }

            match tokio::time::timeout(Duration::from_millis(100), audio_rx.recv()).await {
                Ok(Some(first_chunk)) => {
                    let Some(batch) = build_audio_batch(
                        first_chunk,
                        &mut audio_rx,
                        max_batch_chunks,
                        flush_latency_chunks,
                    ) else {
                        continue;
                    };

                    let chunk_len = batch.bytes.len();
                    send_audio_chunk(&mut write, batch.bytes).await?;
                    sent_audio_since_end_turn = true;
                    total_chunks += batch.chunk_count as u64;
                    total_messages += 1;
                    chunks_since_log += batch.chunk_count as u64;
                    messages_since_log += 1;
                    bytes_since_log += chunk_len;
                    flushed_since_log += batch.flushed_chunks as u64;

                    if last_audio_log.elapsed() >= Duration::from_secs(1) {
                        let dropped = sender_dropped_audio_chunks.swap(0, Ordering::Relaxed);
                        debug_log(format!(
                            "sent audio: total_chunks={} total_messages={} recent_chunks={} recent_messages={} recent_bytes={} last_batch_chunks={} queued_chunks={} flushed_chunks={} dropped_chunks={}",
                            total_chunks,
                            total_messages,
                            chunks_since_log,
                            messages_since_log,
                            bytes_since_log,
                            batch.chunk_count,
                            audio_rx.len(),
                            flushed_since_log,
                            dropped
                        ));
                        chunks_since_log = 0;
                        messages_since_log = 0;
                        bytes_since_log = 0;
                        flushed_since_log = 0;
                        last_audio_log = Instant::now();
                    }
                }
                Ok(None) => break,
                Err(_) => {}
            }

            if let Some(interval) = force_end_turn_after {
                if sent_audio_since_end_turn && last_forced_end_turn.elapsed() >= interval {
                    send_end_turn(&mut write, "forced", &sender_timing).await?;
                    last_forced_end_turn = Instant::now();
                    sent_audio_since_end_turn = false;
                }
            }
        }

        for reason in drain_asr_commands(&command_rx) {
            let _ = send_end_turn(&mut write, &reason, &sender_timing).await;
        }
        let _ = send_end_turn(&mut write, "stop", &sender_timing).await;
        let _ = write
            .send(Message::Text(
                json!({"close_stream": {}}).to_string().into(),
            ))
            .await;
        debug_log("sent close_stream");

        Ok::<(), BoxError>(())
    });

    let receiver_stop = stop.clone();
    let receiver_tx = tx.clone();
    let show_partials = config.show_partials;
    let partial_ui_interval = config.partial_ui_interval;
    let receiver_timing = timing.clone();
    let mut receiver = tokio::spawn(async move {
        let mut last_final = String::new();
        let mut last_partial = String::new();
        let mut last_partial_emit = Instant::now() - partial_ui_interval;

        while !receiver_stop.load(Ordering::Relaxed) {
            match tokio::time::timeout(Duration::from_millis(100), read.next()).await {
                Ok(Some(Ok(Message::Text(text)))) => {
                    if raw_log {
                        debug_raw_log(format!("recv raw {}", text));
                    }
                    handle_server_message(
                        &text,
                        &receiver_tx,
                        show_partials,
                        partial_ui_interval,
                        &mut last_final,
                        &mut last_partial,
                        &mut last_partial_emit,
                        &receiver_timing,
                    );
                }
                Ok(Some(Ok(Message::Binary(bytes)))) => {
                    if let Ok(text) = String::from_utf8(bytes.to_vec()) {
                        if raw_log {
                            debug_raw_log(format!("recv raw {}", text));
                        }
                        handle_server_message(
                            &text,
                            &receiver_tx,
                            show_partials,
                            partial_ui_interval,
                            &mut last_final,
                            &mut last_partial,
                            &mut last_partial_emit,
                            &receiver_timing,
                        );
                    }
                }
                Ok(Some(Ok(Message::Close(close)))) => {
                    debug_log(format!("recv websocket close {:?}", close));
                    if !receiver_stop.load(Ordering::Relaxed) {
                        return Err::<(), BoxError>(
                            format!("Inworld STT websocket closed by server: {:?}", close).into(),
                        );
                    }
                    break;
                }
                Ok(None) => {
                    debug_log("recv websocket ended");
                    if !receiver_stop.load(Ordering::Relaxed) {
                        return Err::<(), BoxError>(
                            "Inworld STT websocket ended before stop request".into(),
                        );
                    }
                    break;
                }
                Ok(Some(Ok(_))) | Err(_) => {}
                Ok(Some(Err(err))) => {
                    debug_log(format!("recv websocket error {}", err));
                    return Err::<(), BoxError>(Box::new(err));
                }
            }
        }

        Ok::<(), BoxError>(())
    });

    tokio::select! {
        result = &mut sender => {
            receiver.abort();
            result??;
        }
        result = &mut receiver => {
            sender.abort();
            result??;
        }
    }

    drop(stream);
    let _ = tx.send(AsrEvent::Stopped);
    debug_log("session stopped");
    Ok(())
}

async fn send_audio_chunk<W>(write: &mut W, chunk: Vec<u8>) -> Result<(), BoxError>
where
    W: futures_util::Sink<Message> + Unpin,
    W::Error: Error + Send + Sync + 'static,
{
    let message = json!({
        "audio_chunk": {
            "content": general_purpose::STANDARD.encode(chunk),
        }
    });

    write
        .send(Message::Text(message.to_string().into()))
        .await?;
    Ok(())
}

fn drain_asr_commands(command_rx: &SharedCommandRx) -> Vec<String> {
    let mut reasons = Vec::new();
    let Ok(command_rx) = command_rx.lock() else {
        return reasons;
    };

    while let Ok(command) = command_rx.try_recv() {
        match command {
            AsrCommand::Flush { reason } => {
                reasons.push(reason);
            }
        }
    }

    reasons
}

fn reconnect_backoff(config: &ReconnectConfig, attempt: usize) -> Duration {
    let exponent = attempt.saturating_sub(1).min(16);
    let factor = 1_u32 << exponent;
    config
        .reconnect_backoff
        .saturating_mul(factor)
        .min(config.reconnect_max_backoff)
}

fn recovering_status(attempt: usize, config: &ReconnectConfig, delay: Duration) -> String {
    format!(
        "восстанавливаю STT... попытка {}/{} через {:.1} сек",
        attempt,
        config.max_reconnects,
        delay.as_secs_f32()
    )
}

async fn wait_reconnect_delay(stop: &AtomicBool, delay: Duration) {
    let started_at = Instant::now();

    while !stop.load(Ordering::Relaxed) {
        let elapsed = started_at.elapsed();
        if elapsed >= delay {
            return;
        }

        let remaining = delay.saturating_sub(elapsed);
        tokio::time::sleep(remaining.min(Duration::from_millis(100))).await;
    }
}

fn recoverable_asr_error(message: impl Into<String>) -> BoxError {
    Box::new(AsrFailure::new(AsrFailureKind::Recoverable, message))
}

fn terminal_asr_error(message: impl Into<String>) -> BoxError {
    Box::new(AsrFailure::new(AsrFailureKind::Terminal, message))
}

fn is_recoverable_asr_error(err: &BoxError) -> bool {
    asr_failure_kind(err) == AsrFailureKind::Recoverable
}

fn asr_failure_kind(err: &BoxError) -> AsrFailureKind {
    if let Some(failure) = err.downcast_ref::<AsrFailure>() {
        return failure.kind;
    }

    classify_asr_error_message(&err.to_string())
}

fn classify_asr_error_message(message: &str) -> AsrFailureKind {
    let message = message.to_ascii_lowercase();
    let terminal_markers = [
        "microphone setup failed",
        "microphone start failed",
        "invalid inworld stt request/auth config",
        "unauthorized",
        "forbidden",
        "http 401",
        "http 403",
        "401 unauthorized",
        "403 forbidden",
    ];

    if terminal_markers
        .iter()
        .any(|marker| message.contains(marker))
    {
        AsrFailureKind::Terminal
    } else {
        AsrFailureKind::Recoverable
    }
}

async fn send_end_turn<W>(
    write: &mut W,
    reason: &str,
    timing: &Arc<Mutex<AsrTiming>>,
) -> Result<(), BoxError>
where
    W: futures_util::Sink<Message> + Unpin,
    W::Error: Error + Send + Sync + 'static,
{
    write
        .send(Message::Text(json!({"end_turn": {}}).to_string().into()))
        .await?;
    if let Ok(mut timing) = timing.lock() {
        timing.last_end_turn = Some(Instant::now());
        timing.end_turn_count += 1;
    }
    debug_log(format!("sent end_turn reason={}", reason));
    Ok(())
}

async fn connect_socks5(
    proxy: &SocksProxy,
    config: &InworldConfig,
) -> Result<Socks5Stream<TcpStream>, BoxError> {
    let target = format!("{}:{}", config.ws_host()?, config.ws_port()?);
    let proxy_addr = format!("{}:{}", proxy.host, proxy.port);

    if let (Some(username), Some(password)) = (&proxy.username, &proxy.password) {
        Ok(Socks5Stream::connect_with_password(
            proxy_addr.as_str(),
            target.as_str(),
            username,
            password,
        )
        .await?)
    } else {
        Ok(Socks5Stream::connect(proxy_addr.as_str(), target.as_str()).await?)
    }
}

#[derive(Default)]
struct AsrTiming {
    last_end_turn: Option<Instant>,
    end_turn_count: u64,
}

fn start_audio_stream(
    config: &InworldConfig,
    audio_tx: mpsc::Sender<Vec<u8>>,
    dropped_audio_chunks: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
    tx: Sender<AsrEvent>,
) -> Result<Stream, BoxError> {
    let host = cpal::default_host();
    let device = select_input_device(&host, config.mic_device.as_deref())?;
    let supported = device.default_input_config()?;
    let sample_format = supported.sample_format();
    let stream_config: StreamConfig = supported.clone().into();
    let input_rate = stream_config.sample_rate.0;
    let channels = stream_config.channels as usize;
    let processor = Arc::new(Mutex::new(AudioProcessor::new(
        input_rate,
        channels,
        config.sample_rate,
        config.chunk_ms,
        audio_tx,
        dropped_audio_chunks,
        stop,
    )));

    let device_name = device
        .name()
        .unwrap_or_else(|_| "default input".to_string());
    let _ = tx.send(AsrEvent::Connecting(format!(
        "microphone:\n{}\ninput: {} Hz, {} channel(s)",
        device_name, input_rate, channels
    )));
    debug_log(format!(
        "microphone device={} input_rate={} channels={} sample_format={:?}",
        device_name, input_rate, channels, sample_format
    ));

    let err_tx = tx.clone();
    let err_fn = move |err| {
        debug_log(format!("microphone stream error: {}", err));
        let _ = err_tx.send(AsrEvent::Error(format!(
            "microphone stream error:\n{}",
            err
        )));
    };

    match sample_format {
        SampleFormat::F32 => {
            let processor = processor.clone();
            Ok(device.build_input_stream(
                &stream_config,
                move |data: &[f32], _| {
                    if let Ok(mut processor) = processor.lock() {
                        processor.push_samples(data, |sample| sample);
                    }
                },
                err_fn,
                None,
            )?)
        }
        SampleFormat::I16 => {
            let processor = processor.clone();
            Ok(device.build_input_stream(
                &stream_config,
                move |data: &[i16], _| {
                    if let Ok(mut processor) = processor.lock() {
                        processor.push_samples(data, |sample| sample as f32 / i16::MAX as f32);
                    }
                },
                err_fn,
                None,
            )?)
        }
        SampleFormat::U16 => {
            let processor = processor.clone();
            Ok(device.build_input_stream(
                &stream_config,
                move |data: &[u16], _| {
                    if let Ok(mut processor) = processor.lock() {
                        processor.push_samples(data, |sample| (sample as f32 - 32768.0) / 32768.0);
                    }
                },
                err_fn,
                None,
            )?)
        }
        SampleFormat::F64 => {
            let processor = processor.clone();
            Ok(device.build_input_stream(
                &stream_config,
                move |data: &[f64], _| {
                    if let Ok(mut processor) = processor.lock() {
                        processor.push_samples(data, |sample| sample as f32);
                    }
                },
                err_fn,
                None,
            )?)
        }
        other => Err(format!("unsupported microphone sample format: {:?}", other).into()),
    }
}

fn select_input_device(
    host: &cpal::Host,
    requested: Option<&str>,
) -> Result<cpal::Device, BoxError> {
    let Some(requested) = requested else {
        return host
            .default_input_device()
            .ok_or_else(|| "no default input device".into());
    };

    if let Ok(index) = requested.parse::<usize>() {
        if let Some(device) = host.input_devices()?.nth(index) {
            return Ok(device);
        }
    }

    for device in host.input_devices()? {
        if device
            .name()
            .map(|name| name.contains(requested))
            .unwrap_or(false)
        {
            return Ok(device);
        }
    }

    Err(format!("input device not found: {}", requested).into())
}

#[derive(Deserialize)]
struct ServerEnvelope {
    result: Option<ServerResult>,
    transcription: Option<TranscriptionPayload>,
    message: Option<String>,
    code: Option<Value>,
}

#[derive(Deserialize)]
struct ServerResult {
    transcription: Option<TranscriptionPayload>,
    speaker: Option<LabelValue>,
    #[serde(rename = "speakerTag", alias = "speaker_tag")]
    speaker_tag: Option<LabelValue>,
    channel: Option<LabelValue>,
    #[serde(rename = "channelTag", alias = "channel_tag")]
    channel_tag: Option<LabelValue>,
    #[serde(default, rename = "wordTimestamps", alias = "word_timestamps")]
    word_timestamps: Vec<WordTimestampPayload>,
    #[serde(default)]
    words: Vec<WordTimestampPayload>,
    #[serde(default)]
    tokens: Vec<WordTimestampPayload>,
}

#[derive(Deserialize)]
struct TranscriptionPayload {
    transcript: Option<String>,
    #[serde(rename = "isFinal", alias = "is_final")]
    is_final: Option<bool>,
    speaker: Option<LabelValue>,
    #[serde(rename = "speakerTag", alias = "speaker_tag")]
    speaker_tag: Option<LabelValue>,
    channel: Option<LabelValue>,
    #[serde(rename = "channelTag", alias = "channel_tag")]
    channel_tag: Option<LabelValue>,
    #[serde(default, rename = "wordTimestamps", alias = "word_timestamps")]
    word_timestamps: Vec<WordTimestampPayload>,
    #[serde(default)]
    words: Vec<WordTimestampPayload>,
    #[serde(default)]
    tokens: Vec<WordTimestampPayload>,
}

#[derive(Deserialize)]
struct WordTimestampPayload {
    speaker: Option<LabelValue>,
    #[serde(rename = "speakerTag", alias = "speaker_tag")]
    speaker_tag: Option<LabelValue>,
    channel: Option<LabelValue>,
    #[serde(rename = "channelTag", alias = "channel_tag")]
    channel_tag: Option<LabelValue>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum LabelValue {
    String(String),
    I64(i64),
    U64(u64),
    F64(f64),
}

#[allow(clippy::too_many_arguments)]
fn handle_server_message(
    raw: &str,
    tx: &Sender<AsrEvent>,
    show_partials: bool,
    partial_ui_interval: Duration,
    last_final: &mut String,
    last_partial: &mut String,
    last_partial_emit: &mut Instant,
    timing: &Arc<Mutex<AsrTiming>>,
) {
    let Ok(envelope) = serde_json::from_str::<ServerEnvelope>(raw) else {
        debug_log("recv invalid json");
        return;
    };

    if envelope.code.is_some() {
        let message = envelope
            .message
            .unwrap_or_else(|| "unknown server error".to_string());
        debug_log(format!("server error: {}", message));
        let _ = tx.send(AsrEvent::Error(format!(
            "Inworld STT server error:\n{}",
            message
        )));
        return;
    }

    let result = envelope.result.as_ref();
    let transcription = result
        .and_then(|result| result.transcription.as_ref())
        .or(envelope.transcription.as_ref());

    let Some(transcription) = transcription else {
        return;
    };

    let Some(text) = transcription
        .transcript
        .as_deref()
        .map(str::trim)
        .filter(|text| !text.is_empty())
    else {
        return;
    };

    let is_final = transcription.is_final.unwrap_or(false);
    let label = transcript_label(transcription)
        .or_else(|| result.and_then(result_label))
        .unwrap_or_default();
    let text = if label.is_empty() {
        text.to_string()
    } else {
        format!("{} {}", label, text)
    };

    if is_final {
        if &text != last_final {
            let latency = timing
                .lock()
                .ok()
                .and_then(|timing| timing.last_end_turn.map(|sent| sent.elapsed().as_millis()));
            debug_log(format!(
                "recv final chars={} latency_after_last_end_turn_ms={}",
                text.chars().count(),
                latency
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "n/a".to_string())
            ));
            let _ = tx.send(AsrEvent::Transcript(text.clone()));
            *last_final = text;
        }
        last_partial.clear();
    } else if show_partials && &text != last_partial {
        let should_emit = last_partial_emit.elapsed() >= partial_ui_interval;

        if should_emit {
            debug_log(format!("recv partial live chars={}", text.chars().count()));
            let _ = tx.send(AsrEvent::PartialTranscript(text.clone()));
            *last_partial_emit = Instant::now();
        }

        *last_partial = text;
    } else if !show_partials {
        debug_log(format!(
            "recv partial suppressed chars={}",
            text.chars().count()
        ));
    }
}

fn transcript_label(transcription: &TranscriptionPayload) -> Option<String> {
    if let Some(speaker) = transcription_speaker(transcription) {
        return Some(format!("Спикер {}:", speaker));
    }

    if let Some(channel) = transcription_channel(transcription) {
        return Some(format!("Канал {}:", channel));
    }

    None
}

fn result_label(result: &ServerResult) -> Option<String> {
    if let Some(speaker) =
        label_value_to_string(result.speaker.as_ref().or(result.speaker_tag.as_ref()))
            .or_else(|| {
                word_label(&result.word_timestamps, |word| {
                    word.speaker.as_ref().or(word.speaker_tag.as_ref())
                })
            })
            .or_else(|| {
                word_label(&result.words, |word| {
                    word.speaker.as_ref().or(word.speaker_tag.as_ref())
                })
            })
            .or_else(|| {
                word_label(&result.tokens, |word| {
                    word.speaker.as_ref().or(word.speaker_tag.as_ref())
                })
            })
    {
        return Some(format!("Спикер {}:", speaker));
    }

    if let Some(channel) =
        label_value_to_string(result.channel.as_ref().or(result.channel_tag.as_ref()))
            .or_else(|| {
                word_label(&result.word_timestamps, |word| {
                    word.channel.as_ref().or(word.channel_tag.as_ref())
                })
            })
            .or_else(|| {
                word_label(&result.words, |word| {
                    word.channel.as_ref().or(word.channel_tag.as_ref())
                })
            })
            .or_else(|| {
                word_label(&result.tokens, |word| {
                    word.channel.as_ref().or(word.channel_tag.as_ref())
                })
            })
    {
        return Some(format!("Канал {}:", channel));
    }

    None
}

fn transcription_speaker(transcription: &TranscriptionPayload) -> Option<String> {
    label_value_to_string(
        transcription
            .speaker
            .as_ref()
            .or(transcription.speaker_tag.as_ref()),
    )
    .or_else(|| {
        word_label(&transcription.word_timestamps, |word| {
            word.speaker.as_ref().or(word.speaker_tag.as_ref())
        })
    })
    .or_else(|| {
        word_label(&transcription.words, |word| {
            word.speaker.as_ref().or(word.speaker_tag.as_ref())
        })
    })
    .or_else(|| {
        word_label(&transcription.tokens, |word| {
            word.speaker.as_ref().or(word.speaker_tag.as_ref())
        })
    })
}

fn transcription_channel(transcription: &TranscriptionPayload) -> Option<String> {
    label_value_to_string(
        transcription
            .channel
            .as_ref()
            .or(transcription.channel_tag.as_ref()),
    )
    .or_else(|| {
        word_label(&transcription.word_timestamps, |word| {
            word.channel.as_ref().or(word.channel_tag.as_ref())
        })
    })
    .or_else(|| {
        word_label(&transcription.words, |word| {
            word.channel.as_ref().or(word.channel_tag.as_ref())
        })
    })
    .or_else(|| {
        word_label(&transcription.tokens, |word| {
            word.channel.as_ref().or(word.channel_tag.as_ref())
        })
    })
}

fn word_label(
    words: &[WordTimestampPayload],
    pick: impl Fn(&WordTimestampPayload) -> Option<&LabelValue>,
) -> Option<String> {
    words
        .iter()
        .find_map(|word| label_value_to_string(pick(word)))
}

fn label_value_to_string(value: Option<&LabelValue>) -> Option<String> {
    match value? {
        LabelValue::String(value) if !value.trim().is_empty() => Some(value.trim().to_string()),
        LabelValue::I64(value) => Some(value.to_string()),
        LabelValue::U64(value) => Some(value.to_string()),
        LabelValue::F64(value) => Some(format!("{:.0}", value)),
        LabelValue::String(_) => None,
    }
}

fn spawn_mock_asr(
    tx: Sender<AsrEvent>,
    stop: Arc<AtomicBool>,
    command_rx: StdReceiver<AsrCommand>,
) {
    thread::spawn(move || {
        debug_log("mock session start");
        let _ = tx.send(AsrEvent::Connecting("Starting mock ASR...".to_string()));
        let _ = tx.send(AsrEvent::Ready("Mock ASR ready".to_string()));

        let chunks = [
            "Mock ASR: add INWORLD_API_KEY to .env to use native Rust Inworld STT.",
            "This fallback is in-process Rust.",
        ];

        let mut i = 0;

        while !stop.load(Ordering::Relaxed) {
            while let Ok(command) = command_rx.try_recv() {
                match command {
                    AsrCommand::Flush { reason } => {
                        debug_log(format!("mock ignored command flush reason={}", reason));
                    }
                }
            }

            let _ = tx.send(AsrEvent::Transcript(chunks[i % chunks.len()].to_string()));
            i += 1;
            thread::sleep(Duration::from_millis(1200));
        }

        let _ = tx.send(AsrEvent::Stopped);
        debug_log("mock session stopped");
    });
}

fn debug_log(message: impl AsRef<str>) {
    if let Some(path) = log_path_from_env("REC_SIDECAR_LOG", DEFAULT_LOG_PATH) {
        write_log_line(&path, message);
    }
}

fn debug_raw_log(message: impl AsRef<str>) {
    if let Some(path) = log_path_from_env("REC_SIDECAR_RAW_LOG", DEFAULT_RAW_LOG_PATH) {
        write_log_line(&path, message);
    }
}

fn log_path_from_env(name: &str, default: &str) -> Option<String> {
    match env::var(name) {
        Ok(value) => {
            let value = value.trim();
            if matches!(
                value.to_ascii_lowercase().as_str(),
                "0" | "false" | "off" | "no"
            ) {
                return None;
            }
            if value.is_empty() {
                Some(default.to_string())
            } else {
                Some(value.to_string())
            }
        }
        Err(_) => Some(default.to_string()),
    }
}

fn write_log_line(path: &str, message: impl AsRef<str>) {
    if let Some(parent) = Path::new(path).parent() {
        let _ = create_dir_all(parent);
    }

    let _guard = LOG_LOCK.get_or_init(|| Mutex::new(())).lock().ok();

    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(
            file,
            "{} {}",
            Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
            message.as_ref()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parser_state() -> (String, String, Instant, Arc<Mutex<AsrTiming>>) {
        (
            String::new(),
            String::new(),
            Instant::now() - Duration::from_secs(1),
            Arc::new(Mutex::new(AsrTiming::default())),
        )
    }

    #[test]
    fn server_final_transcript_adds_speaker_and_suppresses_duplicate() {
        let (tx, rx) = std_mpsc::channel();
        let (mut last_final, mut last_partial, mut last_partial_emit, timing) = parser_state();
        let raw = json!({
            "result": {
                "transcription": {
                    "transcript": "привет",
                    "isFinal": true,
                    "speakerTag": 2
                }
            }
        })
        .to_string();

        handle_server_message(
            &raw,
            &tx,
            true,
            Duration::from_millis(1),
            &mut last_final,
            &mut last_partial,
            &mut last_partial_emit,
            &timing,
        );
        handle_server_message(
            &raw,
            &tx,
            true,
            Duration::from_millis(1),
            &mut last_final,
            &mut last_partial,
            &mut last_partial_emit,
            &timing,
        );

        match rx.try_recv().unwrap() {
            AsrEvent::Transcript(text) => assert_eq!(text, "Спикер 2: привет"),
            _ => panic!("expected final transcript"),
        }
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn server_partial_respects_show_partials() {
        let (tx, rx) = std_mpsc::channel();
        let (mut last_final, mut last_partial, mut last_partial_emit, timing) = parser_state();
        let raw = json!({
            "transcription": {
                "transcript": "live",
                "isFinal": false,
                "channel": "left"
            }
        })
        .to_string();

        handle_server_message(
            &raw,
            &tx,
            true,
            Duration::from_millis(1),
            &mut last_final,
            &mut last_partial,
            &mut last_partial_emit,
            &timing,
        );

        match rx.try_recv().unwrap() {
            AsrEvent::PartialTranscript(text) => assert_eq!(text, "Канал left: live"),
            _ => panic!("expected partial transcript"),
        }
    }

    #[test]
    fn server_error_envelope_emits_error_event() {
        let (tx, rx) = std_mpsc::channel();
        let (mut last_final, mut last_partial, mut last_partial_emit, timing) = parser_state();

        handle_server_message(
            r#"{"code": 13, "message": "bad things"}"#,
            &tx,
            true,
            Duration::from_millis(1),
            &mut last_final,
            &mut last_partial,
            &mut last_partial_emit,
            &timing,
        );

        match rx.try_recv().unwrap() {
            AsrEvent::Error(text) => assert!(text.contains("bad things")),
            _ => panic!("expected error event"),
        }
    }

    #[test]
    fn reconnect_backoff_doubles_and_caps() {
        let config = ReconnectConfig {
            max_reconnects: 4,
            reconnect_backoff: Duration::from_millis(750),
            reconnect_max_backoff: Duration::from_millis(2_000),
            connect_timeout: Duration::from_millis(10_000),
        };

        assert_eq!(reconnect_backoff(&config, 1), Duration::from_millis(750));
        assert_eq!(reconnect_backoff(&config, 2), Duration::from_millis(1_500));
        assert_eq!(reconnect_backoff(&config, 3), Duration::from_millis(2_000));
        assert_eq!(reconnect_backoff(&config, 4), Duration::from_millis(2_000));
    }

    #[test]
    fn asr_error_classification_splits_terminal_and_recoverable() {
        let terminal = terminal_asr_error("microphone setup failed: no default input device");
        assert!(!is_recoverable_asr_error(&terminal));

        let recoverable = recoverable_asr_error("Inworld STT websocket timed out");
        assert!(is_recoverable_asr_error(&recoverable));

        assert_eq!(
            classify_asr_error_message("Inworld STT websocket closed by server"),
            AsrFailureKind::Recoverable
        );
        assert_eq!(
            classify_asr_error_message("HTTP 401 Unauthorized"),
            AsrFailureKind::Terminal
        );
    }

    #[test]
    fn shared_command_receiver_drains_across_reconnect_attempts() {
        let (tx, rx) = std_mpsc::channel();
        let rx = Arc::new(Mutex::new(rx));

        tx.send(AsrCommand::Flush {
            reason: "first".to_string(),
        })
        .unwrap();
        assert_eq!(drain_asr_commands(&rx), vec!["first"]);

        tx.send(AsrCommand::Flush {
            reason: "second".to_string(),
        })
        .unwrap();
        assert_eq!(drain_asr_commands(&rx), vec!["second"]);
    }
}
