use futures_util::StreamExt;
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    env,
    error::Error,
    fs::{create_dir_all, OpenOptions},
    io::Write,
    path::Path,
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::{Duration, Instant},
};

mod streaming;

use streaming::{event_data_lines, take_sse_event};

const DEFAULT_SERVICE_URL: &str = "http://127.0.0.1:8088";
const DEFAULT_INTERVAL_MS: u64 = 2_000;
const DEFAULT_TIMEOUT_SECS: u64 = 30;
const DEFAULT_RATE_LIMIT_BACKOFF_MS: u64 = 15_000;
const DEFAULT_HELP_CONSTRUCTIVE_TIMEOUT_MS: u64 = 20_000;
const DEFAULT_COACH_LOG_PATH: &str = "logs/rec-sidecar.coach.log";
const HELP_CONSTRUCTIVE_STREAM_PREFIX: &str = "\n**Следующий ход:**\n";

type BoxError = Box<dyn Error + Send + Sync>;

pub enum CoachInput {
    Snapshot(CoachSnapshot),
    Chat(CoachChatRequest),
    Help(CoachHelpRequest),
    Stage(CoachStageRequest),
    Stop,
}

#[derive(Clone)]
pub struct CoachSnapshot {
    pub run_id: String,
    pub content: String,
    pub force: bool,
}

pub struct CoachChatRequest {
    pub id: u64,
    pub run_id: String,
    pub question: String,
    pub context: String,
}

#[derive(Clone)]
pub struct CoachHelpRequest {
    pub id: u64,
    pub run_id: String,
    pub context: String,
}

#[derive(Clone)]
pub struct CoachStageRequest {
    pub run_id: String,
    pub context: String,
    pub current_stage: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CoachStageAgenda {
    pub stage: String,
    pub title: String,
    pub agenda: String,
    pub emotion: String,
    pub step: String,
    pub provider: String,
    pub model: String,
    pub scorecard: Option<CoachStageScorecard>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CoachStageScorecard {
    pub readiness: String,
    pub readiness_label: String,
    pub score: Option<f32>,
    pub hit_count: u32,
    pub miss_count: u32,
    pub total_count: u32,
    pub hard_red: bool,
    pub ready_to_advance: bool,
    pub next_action: String,
    pub summary: String,
    #[serde(default)]
    pub checks: Vec<CoachStageScoreCheck>,
    #[serde(default)]
    pub signals: Vec<CoachStageScoreSignal>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CoachStageScoreCheck {
    pub id: String,
    pub label: String,
    pub level: String,
    pub result: String,
    pub signal: String,
    pub reason: String,
    #[serde(default)]
    pub evidence: Vec<CoachStageScoreEvidence>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CoachStageScoreEvidence {
    pub speaker: Option<String>,
    pub quote: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CoachStageScoreSignal {
    pub id: String,
    pub label: String,
    pub state: String,
    pub detail: String,
}

pub enum CoachEvent {
    Ready(String),
    Started,
    Delta(String),
    Finished,
    StageAgenda(Box<CoachStageAgenda>),
    StageError(String),
    HelpStage(u64, CoachHelpStage),
    ChatModel(u64, String),
    ChatStarted(u64),
    ChatDelta(u64, String),
    ChatFinished(u64),
    ChatError(u64, String),
    Error(String),
    Stopped,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoachHelpStage {
    FreezingContext,
    PreparingOpener,
    PreparingConstructive,
    Ready,
    Fallback,
    Error,
}

impl CoachHelpStage {
    pub fn label(self) -> &'static str {
        match self {
            Self::FreezingContext => "фиксирую контекст",
            Self::PreparingOpener => "готовлю фразу",
            Self::PreparingConstructive => "дополняю следующий ход",
            Self::Ready => "готово",
            Self::Fallback => "fallback",
            Self::Error => "ошибка",
        }
    }

    pub fn status(self) -> String {
        format!("Помоги: {}", self.label())
    }
}

#[derive(Clone, Default)]
pub struct CoachSettings;

#[derive(Clone)]
struct CoachConfig {
    service_url: String,
    service_token: Option<String>,
    interval: Duration,
    rate_limit_backoff: Duration,
    timeout: Duration,
    help_constructive_timeout: Duration,
}

#[derive(Deserialize)]
struct HealthResponse {
    status: String,
    provider: String,
    model: String,
}

#[derive(Deserialize)]
struct LiveResponse {
    action: String,
    text: String,
    provider: Option<String>,
    model: Option<String>,
}

#[derive(Deserialize)]
struct StageAgendaResponse {
    stage: String,
    title: String,
    agenda: String,
    emotion: String,
    step: String,
    provider: String,
    model: String,
    #[allow(dead_code)]
    confidence: Option<f32>,
    #[serde(default)]
    scorecard: Option<CoachStageScorecard>,
}

struct HelpOpenerOutcome {
    emitted: bool,
    fallback: bool,
}

#[derive(Deserialize)]
struct ServiceStreamEvent {
    event: String,
    text: Option<String>,
    provider: Option<String>,
    model: Option<String>,
    message: Option<String>,
}

#[derive(Default)]
struct ServiceStreamResult {
    emitted: bool,
    fallback: bool,
}

#[derive(Debug, PartialEq, Eq)]
enum ServiceStreamAction {
    Continue,
    Done,
}

pub fn spawn_coach_worker(
    tx: Sender<CoachEvent>,
    settings: CoachSettings,
) -> Option<Sender<CoachInput>> {
    let config = CoachConfig::from_env(settings);

    let (input_tx, input_rx) = mpsc::channel();

    thread::spawn(move || {
        if let Err(err) = run_coach_worker(config, input_rx, tx.clone()) {
            coach_log(format!("worker error: {}", err));
            let _ = tx.send(CoachEvent::Error(format!("Coach worker error: {}", err)));
        }

        let _ = tx.send(CoachEvent::Stopped);
    });

    Some(input_tx)
}

pub fn log_event(message: impl AsRef<str>) {
    coach_log(message);
}

fn run_coach_worker(
    config: CoachConfig,
    input_rx: Receiver<CoachInput>,
    tx: Sender<CoachEvent>,
) -> Result<(), BoxError> {
    coach_log(config.debug_summary());

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let client = build_client(&config)?;

    match runtime.block_on(fetch_health(&client, &config)) {
        Ok(health) => {
            let _ = tx.send(CoachEvent::Ready(format!(
                "Coach ready\nservice: {}\nprovider: {}\nmodel: {}\nstatus: {}",
                config.service_url, health.provider, health.model, health.status
            )));
        }
        Err(err) => {
            coach_log(format!("service health check failed: {}", err));
            let _ = tx.send(CoachEvent::Error(format!(
                "LLM service unavailable at {}: {}",
                config.service_url,
                concise_error(&err)
            )));
        }
    }

    let mut latest: Option<CoachSnapshot> = None;
    let mut pending_stage: Option<CoachStageRequest> = None;
    let mut pending_helps = VecDeque::new();
    let mut pending_chats = VecDeque::new();
    let mut last_sent = String::new();
    let mut force_due = false;
    let mut next_due = Instant::now();

    loop {
        let timeout = if force_due
            || pending_stage.is_some()
            || !pending_helps.is_empty()
            || !pending_chats.is_empty()
        {
            Duration::ZERO
        } else {
            next_due.saturating_duration_since(Instant::now())
        };

        match input_rx.recv_timeout(timeout) {
            Ok(CoachInput::Snapshot(snapshot)) => {
                force_due |= snapshot.force;
                latest = Some(snapshot);
                continue;
            }
            Ok(CoachInput::Chat(request)) => {
                pending_chats.push_back(request);
                continue;
            }
            Ok(CoachInput::Help(request)) => {
                pending_helps.push_back(request);
                continue;
            }
            Ok(CoachInput::Stage(request)) => {
                pending_stage = Some(request);
                continue;
            }
            Ok(CoachInput::Stop) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }

        if drain_inputs(
            &input_rx,
            &mut latest,
            &mut pending_stage,
            &mut pending_helps,
            &mut pending_chats,
            &mut force_due,
        ) {
            break;
        }

        while let Some(request) = pending_helps.pop_front() {
            let started_at = Instant::now();
            coach_log(format!(
                "help request start id={} context_chars={}",
                request.id,
                request.context.chars().count(),
            ));

            match runtime.block_on(send_help_request(&client, &config, &request, &tx)) {
                Ok(()) => {
                    coach_log(format!(
                        "help request done id={} elapsed_ms={}",
                        request.id,
                        started_at.elapsed().as_millis()
                    ));
                }
                Err(err) => {
                    let message = format!("Coach help error: {}", concise_error(&err));
                    coach_log(format!("help request error id={}: {}", request.id, err));
                    let _ = tx.send(CoachEvent::ChatError(request.id, message));
                }
            }

            if drain_inputs(
                &input_rx,
                &mut latest,
                &mut pending_stage,
                &mut pending_helps,
                &mut pending_chats,
                &mut force_due,
            ) {
                return Ok(());
            }
        }

        while let Some(request) = pending_chats.pop_front() {
            let started_at = Instant::now();
            coach_log(format!(
                "chat request start id={} question_chars={} context_chars={}",
                request.id,
                request.question.chars().count(),
                request.context.chars().count(),
            ));

            match runtime.block_on(send_chat_request(&client, &config, &request, &tx)) {
                Ok(()) => {
                    coach_log(format!(
                        "chat request done id={} elapsed_ms={}",
                        request.id,
                        started_at.elapsed().as_millis()
                    ));
                }
                Err(err) => {
                    let message = format!("Coach chat error: {}", concise_error(&err));
                    coach_log(format!("chat request error id={}: {}", request.id, err));
                    let _ = tx.send(CoachEvent::ChatError(request.id, message));
                }
            }

            if drain_inputs(
                &input_rx,
                &mut latest,
                &mut pending_stage,
                &mut pending_helps,
                &mut pending_chats,
                &mut force_due,
            ) {
                return Ok(());
            }
        }

        if let Some(request) = pending_stage.take() {
            let started_at = Instant::now();
            coach_log(format!(
                "stage request start run_id={} context_chars={} current_stage={}",
                request.run_id,
                request.context.chars().count(),
                request.current_stage.as_deref().unwrap_or("unknown"),
            ));

            match runtime.block_on(send_stage_request(&client, &config, &request, &tx)) {
                Ok(()) => {
                    coach_log(format!(
                        "stage request done run_id={} elapsed_ms={}",
                        request.run_id,
                        started_at.elapsed().as_millis()
                    ));
                }
                Err(err) => {
                    coach_log(format!(
                        "stage request error run_id={}: {}",
                        request.run_id, err
                    ));
                    let _ = tx.send(CoachEvent::StageError(format!(
                        "Stage detect error: {}",
                        concise_error(&err)
                    )));
                }
            }

            if drain_inputs(
                &input_rx,
                &mut latest,
                &mut pending_stage,
                &mut pending_helps,
                &mut pending_chats,
                &mut force_due,
            ) {
                return Ok(());
            }
        }

        let Some(snapshot) = latest.clone() else {
            next_due = Instant::now() + config.interval;
            continue;
        };

        let snapshot_id = snapshot.content.clone();
        if snapshot_id.trim().is_empty() || (!force_due && snapshot_id == last_sent) {
            next_due = Instant::now() + config.interval;
            force_due = false;
            continue;
        }

        let started_at = Instant::now();
        coach_log(format!(
            "live request start chars={} force={}",
            snapshot.content.chars().count(),
            force_due,
        ));

        match runtime.block_on(send_coach_request(&client, &config, &snapshot, &tx)) {
            Ok(showed_suggestion) => {
                coach_log(format!(
                    "live request done showed_suggestion={} elapsed_ms={}",
                    showed_suggestion,
                    started_at.elapsed().as_millis()
                ));
                last_sent = snapshot_id;
            }
            Err(err) => {
                coach_log(format!("live request error: {}", err));
                let _ = tx.send(CoachEvent::Error(format!(
                    "Coach error: {}",
                    concise_error(&err)
                )));
                if is_rate_limit_error(&err) {
                    next_due = Instant::now() + config.rate_limit_backoff;
                    last_sent = snapshot_id;
                    force_due = false;
                    continue;
                }
            }
        }

        force_due = false;
        if drain_inputs(
            &input_rx,
            &mut latest,
            &mut pending_stage,
            &mut pending_helps,
            &mut pending_chats,
            &mut force_due,
        ) {
            break;
        }
        next_due = Instant::now() + config.interval;
    }

    Ok(())
}

fn drain_inputs(
    input_rx: &Receiver<CoachInput>,
    latest: &mut Option<CoachSnapshot>,
    pending_stage: &mut Option<CoachStageRequest>,
    pending_helps: &mut VecDeque<CoachHelpRequest>,
    pending_chats: &mut VecDeque<CoachChatRequest>,
    force_due: &mut bool,
) -> bool {
    while let Ok(input) = input_rx.try_recv() {
        match input {
            CoachInput::Snapshot(snapshot) => {
                *force_due |= snapshot.force;
                *latest = Some(snapshot);
            }
            CoachInput::Chat(request) => {
                pending_chats.push_back(request);
            }
            CoachInput::Help(request) => {
                pending_helps.push_back(request);
            }
            CoachInput::Stage(request) => {
                *pending_stage = Some(request);
            }
            CoachInput::Stop => return true,
        }
    }

    false
}

async fn fetch_health(client: &Client, config: &CoachConfig) -> Result<HealthResponse, BoxError> {
    let response = apply_service_auth(
        client.get(service_url(config, "/healthz")),
        config.service_token.as_deref(),
    )
    .send()
    .await?;
    ensure_success(response)
        .await?
        .json::<HealthResponse>()
        .await
        .map_err(Into::into)
}

async fn send_stage_request(
    client: &Client,
    config: &CoachConfig,
    request: &CoachStageRequest,
    tx: &Sender<CoachEvent>,
) -> Result<(), BoxError> {
    let response = service_post(
        client,
        config,
        "/v1/coach/stage",
        stage_request_body(request),
    )
    .await?;
    let agenda = response.json::<StageAgendaResponse>().await?;
    coach_log(format!(
        "stage response run_id={} stage={} provider={} model={}",
        request.run_id, agenda.stage, agenda.provider, agenda.model
    ));
    let _ = tx.send(CoachEvent::StageAgenda(Box::new(CoachStageAgenda {
        stage: agenda.stage,
        title: agenda.title,
        agenda: agenda.agenda,
        emotion: agenda.emotion,
        step: agenda.step,
        provider: agenda.provider,
        model: agenda.model,
        scorecard: agenda.scorecard,
    })));
    Ok(())
}

async fn send_help_request(
    client: &Client,
    config: &CoachConfig,
    request: &CoachHelpRequest,
    tx: &Sender<CoachEvent>,
) -> Result<(), BoxError> {
    let _ = tx.send(CoachEvent::ChatStarted(request.id));
    let _ = tx.send(CoachEvent::HelpStage(
        request.id,
        CoachHelpStage::PreparingOpener,
    ));
    let (slow_handle, slow_rx) = spawn_help_constructive_task(client, config, request);

    let opener = match send_help_opener_request(client, config, request, tx).await {
        Ok(opener) => opener,
        Err(err) => {
            coach_log(format!(
                "help opener service failed id={}: {}",
                request.id,
                concise_error(&err)
            ));
            HelpOpenerOutcome {
                emitted: emit_help_opener_text(tx, request.id, fallback_help_opener_text()),
                fallback: true,
            }
        }
    };
    let opener_emitted = opener.emitted;
    if opener.fallback {
        let _ = tx.send(CoachEvent::HelpStage(request.id, CoachHelpStage::Fallback));
    }

    let _ = tx.send(CoachEvent::HelpStage(
        request.id,
        CoachHelpStage::PreparingConstructive,
    ));

    forward_coach_events(&slow_rx, tx);

    match finish_help_constructive_task(slow_handle, &slow_rx, tx, config.help_constructive_timeout)
        .await
    {
        Ok(true) => {
            let _ = tx.send(CoachEvent::HelpStage(request.id, CoachHelpStage::Ready));
            let _ = tx.send(CoachEvent::ChatFinished(request.id));
            Ok(())
        }
        Ok(false) => {
            let _ = tx.send(CoachEvent::HelpStage(request.id, CoachHelpStage::Fallback));
            let _ = tx.send(CoachEvent::ChatDelta(
                request.id,
                fallback_constructive_text().to_string(),
            ));
            let _ = tx.send(CoachEvent::ChatFinished(request.id));
            Ok(())
        }
        Err(err) if opener_emitted => {
            coach_log(format!(
                "help constructive failed id={}: {}",
                request.id,
                concise_error(&err)
            ));
            let _ = tx.send(CoachEvent::HelpStage(request.id, CoachHelpStage::Fallback));
            let _ = tx.send(CoachEvent::ChatDelta(
                request.id,
                fallback_constructive_error_text(&concise_error(&err)),
            ));
            let _ = tx.send(CoachEvent::ChatFinished(request.id));
            Ok(())
        }
        Err(err) => {
            let _ = tx.send(CoachEvent::HelpStage(request.id, CoachHelpStage::Error));
            Err(err)
        }
    }
}

fn spawn_help_constructive_task(
    client: &Client,
    config: &CoachConfig,
    request: &CoachHelpRequest,
) -> (
    tokio::task::JoinHandle<Result<bool, BoxError>>,
    Receiver<CoachEvent>,
) {
    let (slow_tx, slow_rx) = mpsc::channel();
    let slow_client = client.clone();
    let slow_config = config.clone();
    let slow_request = request.clone();
    let handle = tokio::spawn(async move {
        let started_at = Instant::now();
        let result =
            send_help_constructive_request(&slow_client, &slow_config, &slow_request, &slow_tx)
                .await;
        coach_log(format!(
            "help constructive done id={} elapsed_ms={} success={}",
            slow_request.id,
            started_at.elapsed().as_millis(),
            result.as_ref().map(|shown| *shown).unwrap_or(false)
        ));
        result
    });

    (handle, slow_rx)
}

async fn finish_help_constructive_task(
    handle: tokio::task::JoinHandle<Result<bool, BoxError>>,
    slow_rx: &Receiver<CoachEvent>,
    tx: &Sender<CoachEvent>,
    timeout: Duration,
) -> Result<bool, BoxError> {
    let started_at = Instant::now();

    while !handle.is_finished() {
        forward_coach_events(slow_rx, tx);
        if started_at.elapsed() >= timeout {
            handle.abort();
            forward_coach_events(slow_rx, tx);
            return Err(format!(
                "help constructive timed out after {} ms",
                timeout.as_millis()
            )
            .into());
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }

    let result = handle
        .await
        .map_err(|err| format!("help constructive task failed: {}", err))?;
    forward_coach_events(slow_rx, tx);
    result
}

fn forward_coach_events(rx: &Receiver<CoachEvent>, tx: &Sender<CoachEvent>) {
    while let Ok(event) = rx.try_recv() {
        let _ = tx.send(event);
    }
}

async fn send_help_opener_request(
    client: &Client,
    config: &CoachConfig,
    request: &CoachHelpRequest,
    tx: &Sender<CoachEvent>,
) -> Result<HelpOpenerOutcome, BoxError> {
    let response = service_post(
        client,
        config,
        "/v1/coach/help/opener/stream",
        help_request_body(request),
    )
    .await?;
    let result =
        parse_service_stream_deltas(response, request.id, tx, Some("**Сказать сейчас:**\n> "))
            .await?;

    if !result.emitted {
        return Err("empty LLM service opener response".into());
    }

    let _ = tx.send(CoachEvent::ChatDelta(request.id, "\n\n".to_string()));
    Ok(HelpOpenerOutcome {
        emitted: result.emitted,
        fallback: result.fallback,
    })
}

fn emit_help_opener_text(tx: &Sender<CoachEvent>, request_id: u64, text: &str) -> bool {
    let Some(text) = format_help_opener_text(text) else {
        return false;
    };

    let _ = tx.send(CoachEvent::ChatDelta(request_id, text));
    true
}

fn format_help_opener_text(text: &str) -> Option<String> {
    let text = text.trim();
    if text.is_empty() {
        return None;
    }

    Some(format!("**Сказать сейчас:**\n> {}\n\n", text))
}

fn fallback_constructive_text() -> &'static str {
    "\n**Следующий ход:** Задай короткий уточняющий вопрос о главном риске клиента и дай ему договорить."
}

fn fallback_constructive_error_text(reason: &str) -> String {
    format!(
        "\n**Следующий ход:** Задай короткий уточняющий вопрос о главном риске клиента и дай ему договорить, потому что конструктив не успел подготовиться: {}.",
        reason.trim()
    )
}

fn fallback_help_opener_text() -> &'static str {
    "Понимаю, вопрос важный, и правильно, что вы его сейчас поднимаете."
}

async fn send_help_constructive_request(
    client: &Client,
    config: &CoachConfig,
    request: &CoachHelpRequest,
    tx: &Sender<CoachEvent>,
) -> Result<bool, BoxError> {
    let response = service_post(
        client,
        config,
        "/v1/coach/help/constructive/stream",
        help_request_body(request),
    )
    .await?;

    parse_service_chat_stream_deltas(
        response,
        request.id,
        tx,
        Some(HELP_CONSTRUCTIVE_STREAM_PREFIX),
    )
    .await
}

async fn send_chat_request(
    client: &Client,
    config: &CoachConfig,
    request: &CoachChatRequest,
    tx: &Sender<CoachEvent>,
) -> Result<(), BoxError> {
    let _ = tx.send(CoachEvent::ChatStarted(request.id));
    let response = service_post(
        client,
        config,
        "/v1/coach/chat/stream",
        chat_request_body(request),
    )
    .await?;
    let emitted = parse_service_chat_stream_deltas(response, request.id, tx, None).await?;

    if !emitted {
        return Err("empty LLM service chat response".into());
    }

    let _ = tx.send(CoachEvent::ChatFinished(request.id));
    Ok(())
}

async fn send_coach_request(
    client: &Client,
    config: &CoachConfig,
    snapshot: &CoachSnapshot,
    tx: &Sender<CoachEvent>,
) -> Result<bool, BoxError> {
    let response = service_post(
        client,
        config,
        "/v1/coach/live",
        live_request_body(snapshot),
    )
    .await?;
    let suggestion = response.json::<LiveResponse>().await?;

    if let Some(provider) = suggestion.provider.as_deref() {
        if let Some(model) = suggestion.model.as_deref() {
            coach_log(format!(
                "live response provider={} model={}",
                provider, model
            ));
        }
    }

    if suggestion.action == "suggest" && !suggestion.text.trim().is_empty() {
        let _ = tx.send(CoachEvent::Started);
        let _ = tx.send(CoachEvent::Delta(suggestion.text.trim().to_string()));
        let _ = tx.send(CoachEvent::Finished);
        Ok(true)
    } else {
        Ok(false)
    }
}

async fn service_post(
    client: &Client,
    config: &CoachConfig,
    path: &str,
    body: Value,
) -> Result<reqwest::Response, BoxError> {
    let builder = client.post(service_url(config, path)).json(&body);
    let response = apply_service_auth(builder, config.service_token.as_deref())
        .send()
        .await?;
    ensure_success(response).await
}

fn apply_service_auth(
    builder: reqwest::RequestBuilder,
    token: Option<&str>,
) -> reqwest::RequestBuilder {
    if let Some(token) = token {
        builder.bearer_auth(token)
    } else {
        builder
    }
}

async fn ensure_success(response: reqwest::Response) -> Result<reqwest::Response, BoxError> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }

    let text = response.text().await.unwrap_or_default();
    Err(format!("LLM service HTTP {}: {}", status, text).into())
}

async fn parse_service_chat_stream_deltas(
    response: reqwest::Response,
    request_id: u64,
    tx: &Sender<CoachEvent>,
    first_delta_prefix: Option<&str>,
) -> Result<bool, BoxError> {
    Ok(
        parse_service_stream_deltas(response, request_id, tx, first_delta_prefix)
            .await?
            .emitted,
    )
}

async fn parse_service_stream_deltas(
    response: reqwest::Response,
    request_id: u64,
    tx: &Sender<CoachEvent>,
    first_delta_prefix: Option<&str>,
) -> Result<ServiceStreamResult, BoxError> {
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    let mut result = ServiceStreamResult::default();
    let mut prefix_sent = false;

    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        buffer.push_str(&String::from_utf8_lossy(&chunk));

        while let Some(event) = take_sse_event(&mut buffer) {
            for data in event_data_lines(&event) {
                if handle_service_stream_event(
                    &data,
                    request_id,
                    tx,
                    first_delta_prefix,
                    &mut prefix_sent,
                    &mut result,
                )? == ServiceStreamAction::Done
                {
                    return Ok(result);
                }
            }
        }
    }

    Ok(result)
}

fn handle_service_stream_event(
    data: &str,
    request_id: u64,
    tx: &Sender<CoachEvent>,
    first_delta_prefix: Option<&str>,
    prefix_sent: &mut bool,
    result: &mut ServiceStreamResult,
) -> Result<ServiceStreamAction, BoxError> {
    let data = data.trim();
    if data.is_empty() {
        return Ok(ServiceStreamAction::Continue);
    }
    if data == "[DONE]" {
        return Ok(ServiceStreamAction::Done);
    }

    let event = serde_json::from_str::<ServiceStreamEvent>(data)?;
    match event.event.as_str() {
        "model" => {
            let provider = event.provider.as_deref().unwrap_or("unknown");
            if let Some(model) = event
                .model
                .as_deref()
                .filter(|model| !model.trim().is_empty())
            {
                coach_log(format!(
                    "service stream model id={} provider={} model={}",
                    request_id,
                    provider,
                    model.trim()
                ));
                let _ = tx.send(CoachEvent::ChatModel(request_id, model.trim().to_string()));
            }
        }
        "delta" => {
            if let Some(text) = event.text.as_deref().filter(|text| !text.is_empty()) {
                if !*prefix_sent {
                    if let Some(prefix) = first_delta_prefix {
                        let _ = tx.send(CoachEvent::ChatDelta(request_id, prefix.to_string()));
                    }
                    *prefix_sent = true;
                }
                result.emitted = true;
                let _ = tx.send(CoachEvent::ChatDelta(request_id, text.to_string()));
            }
        }
        "fallback" => {
            result.fallback = true;
        }
        "done" => return Ok(ServiceStreamAction::Done),
        "error" => {
            let message = event
                .message
                .as_deref()
                .filter(|message| !message.trim().is_empty())
                .unwrap_or("LLM service stream error");
            return Err(message.to_string().into());
        }
        other => coach_log(format!("unknown service stream event: {}", other)),
    }

    Ok(ServiceStreamAction::Continue)
}

fn live_request_body(snapshot: &CoachSnapshot) -> Value {
    json!({
        "run_id": snapshot.run_id,
        "content": snapshot.content,
        "force": snapshot.force,
    })
}

fn chat_request_body(request: &CoachChatRequest) -> Value {
    json!({
        "id": request.id,
        "run_id": request.run_id,
        "context": request.context,
        "question": request.question,
    })
}

fn help_request_body(request: &CoachHelpRequest) -> Value {
    json!({
        "id": request.id,
        "run_id": request.run_id,
        "context": request.context,
    })
}

fn stage_request_body(request: &CoachStageRequest) -> Value {
    json!({
        "run_id": request.run_id,
        "context": request.context,
        "current_stage": request.current_stage,
    })
}

fn build_client(config: &CoachConfig) -> Result<Client, BoxError> {
    Ok(Client::builder().timeout(config.timeout).build()?)
}

fn service_url(config: &CoachConfig, path: &str) -> String {
    format!(
        "{}{}",
        config.service_url.trim_end_matches('/'),
        if path.starts_with('/') {
            path.to_string()
        } else {
            format!("/{path}")
        }
    )
}

fn is_rate_limit_error(err: &BoxError) -> bool {
    let message = err.to_string().to_ascii_lowercase();
    message.contains("http 429")
        || message.contains("too many requests")
        || message.contains("rate limit")
        || message.contains("rate_limit")
        || message.contains("quota")
        || message.contains("resource_exhausted")
        || message.contains("exceeded")
}

impl CoachConfig {
    fn from_env(_settings: CoachSettings) -> Self {
        let service_url =
            env_var("COACH_LLM_SERVICE_URL").unwrap_or_else(|| DEFAULT_SERVICE_URL.to_string());
        let service_token = env_var("COACH_LLM_SERVICE_TOKEN");
        let interval = Duration::from_millis(env_u64_any(
            &["COACH_INTERVAL_MS", "CEREBRAS_COACH_INTERVAL_MS"],
            DEFAULT_INTERVAL_MS,
        ));
        let rate_limit_backoff = Duration::from_millis(env_u64_any(
            &[
                "COACH_RATE_LIMIT_BACKOFF_MS",
                "CEREBRAS_RATE_LIMIT_BACKOFF_MS",
            ],
            DEFAULT_RATE_LIMIT_BACKOFF_MS,
        ));
        let timeout = Duration::from_secs(env_u64_any(
            &["COACH_LLM_SERVICE_TIMEOUT_SECS", "CEREBRAS_TIMEOUT_SECS"],
            DEFAULT_TIMEOUT_SECS,
        ));
        let help_constructive_timeout = Duration::from_millis(env_u64(
            "COACH_HELP_CONSTRUCTIVE_TIMEOUT_MS",
            DEFAULT_HELP_CONSTRUCTIVE_TIMEOUT_MS,
        ));

        Self {
            service_url,
            service_token,
            interval,
            rate_limit_backoff,
            timeout,
            help_constructive_timeout,
        }
    }

    fn debug_summary(&self) -> String {
        format!(
            "config llm_service_url={} interval_ms={} rate_limit_backoff_ms={} timeout_secs={} help_constructive_timeout_ms={} service_auth={}",
            self.service_url,
            self.interval.as_millis(),
            self.rate_limit_backoff.as_millis(),
            self.timeout.as_secs(),
            self.help_constructive_timeout.as_millis(),
            if self.service_token.is_some() { "on" } else { "off" },
        )
    }
}

fn env_var(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn env_u64(name: &str, default: u64) -> u64 {
    env_var(name)
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn env_u64_any(names: &[&str], default: u64) -> u64 {
    names
        .iter()
        .find_map(|name| env_var(name).and_then(|value| value.parse().ok()))
        .unwrap_or(default)
}

fn concise_error(err: &BoxError) -> String {
    let message = err.to_string();

    if message.contains("HTTP 429") {
        return "rate limit, backing off".to_string();
    }

    if message.contains("Connection refused") || message.contains("error sending request") {
        return "LLM service is not reachable; start it with docker compose up --build llm-service"
            .to_string();
    }

    if let Some((head, _)) = message.split_once(": {") {
        return head.to_string();
    }

    message.chars().take(240).collect()
}

fn coach_log(message: impl AsRef<str>) {
    let path = env::var("REC_SIDECAR_COACH_LOG")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_COACH_LOG_PATH.to_string());

    if matches!(
        path.to_ascii_lowercase().as_str(),
        "0" | "false" | "off" | "no"
    ) {
        return;
    }

    if let Some(parent) = Path::new(&path).parent() {
        let _ = create_dir_all(parent);
    }

    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(
            file,
            "{} {}",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
            message.as_ref()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn help_stage_labels_are_stable() {
        assert_eq!(CoachHelpStage::FreezingContext.label(), "фиксирую контекст");
        assert_eq!(
            CoachHelpStage::PreparingOpener.status(),
            "Помоги: готовлю фразу"
        );
        assert_eq!(
            CoachHelpStage::PreparingConstructive.status(),
            "Помоги: дополняю следующий ход"
        );
        assert_eq!(CoachHelpStage::Ready.label(), "готово");
        assert_eq!(CoachHelpStage::Fallback.label(), "fallback");
        assert_eq!(CoachHelpStage::Error.label(), "ошибка");
    }

    #[test]
    fn help_opener_format_wraps_read_aloud_text() {
        assert_eq!(
            format_help_opener_text("  Скажите спокойно.  ").as_deref(),
            Some("**Сказать сейчас:**\n> Скажите спокойно.\n\n")
        );
        assert!(format_help_opener_text("  ").is_none());
    }

    #[test]
    fn fallback_constructive_text_keeps_next_step_shape() {
        let text = fallback_constructive_error_text("timeout");

        assert!(text.contains("**Следующий ход:**"));
        assert!(!text.contains("_Комментарий:_"));
        assert!(text.contains("timeout"));
    }

    #[test]
    fn live_request_body_matches_service_contract() {
        let body = live_request_body(&CoachSnapshot {
            run_id: "run-1".to_string(),
            content: "Спикер 1: привет".to_string(),
            force: true,
        });

        assert_eq!(body["run_id"], "run-1");
        assert_eq!(body["content"], "Спикер 1: привет");
        assert_eq!(body["force"], true);
    }

    #[test]
    fn stage_request_body_matches_service_contract() {
        let body = stage_request_body(&CoachStageRequest {
            run_id: "run-1".to_string(),
            context: "dialogue".to_string(),
            current_stage: Some("S2.2".to_string()),
        });

        assert_eq!(body["run_id"], "run-1");
        assert_eq!(body["context"], "dialogue");
        assert_eq!(body["current_stage"], "S2.2");
    }

    #[test]
    fn service_stream_event_maps_model_delta_and_done() {
        let (tx, rx) = mpsc::channel();
        let mut prefix_sent = false;
        let mut result = ServiceStreamResult::default();

        let action = handle_service_stream_event(
            r#"{"event":"model","provider":"vertex","model":"gemini"}"#,
            7,
            &tx,
            Some("prefix: "),
            &mut prefix_sent,
            &mut result,
        )
        .unwrap();
        assert_eq!(action, ServiceStreamAction::Continue);

        let action = handle_service_stream_event(
            r#"{"event":"delta","text":"hello"}"#,
            7,
            &tx,
            Some("prefix: "),
            &mut prefix_sent,
            &mut result,
        )
        .unwrap();
        assert_eq!(action, ServiceStreamAction::Continue);
        assert!(result.emitted);
        assert!(prefix_sent);

        let action = handle_service_stream_event(
            r#"{"event":"done"}"#,
            7,
            &tx,
            Some("prefix: "),
            &mut prefix_sent,
            &mut result,
        )
        .unwrap();
        assert_eq!(action, ServiceStreamAction::Done);

        assert!(
            matches!(rx.try_recv().unwrap(), CoachEvent::ChatModel(7, model) if model == "gemini")
        );
        assert!(
            matches!(rx.try_recv().unwrap(), CoachEvent::ChatDelta(7, text) if text == "prefix: ")
        );
        assert!(
            matches!(rx.try_recv().unwrap(), CoachEvent::ChatDelta(7, text) if text == "hello")
        );
    }

    #[test]
    fn service_stream_event_marks_fallback() {
        let (tx, _rx) = mpsc::channel();
        let mut prefix_sent = false;
        let mut result = ServiceStreamResult::default();

        let action = handle_service_stream_event(
            r#"{"event":"fallback"}"#,
            7,
            &tx,
            None,
            &mut prefix_sent,
            &mut result,
        )
        .unwrap();

        assert_eq!(action, ServiceStreamAction::Continue);
        assert!(result.fallback);
        assert!(!result.emitted);
    }

    #[tokio::test]
    async fn help_constructive_timeout_returns_error() {
        let (tx, rx) = mpsc::channel();
        let handle = tokio::spawn(async {
            tokio::time::sleep(Duration::from_secs(60)).await;
            Ok::<bool, BoxError>(true)
        });

        let err = finish_help_constructive_task(handle, &rx, &tx, Duration::from_millis(1))
            .await
            .unwrap_err();

        assert!(err.to_string().contains("help constructive timed out"));
    }
}
