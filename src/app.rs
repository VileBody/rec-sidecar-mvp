use crate::{
    asr, coach,
    context::{self, CoachChatMessage, CoachChatRole, ContextInput, HelpContextSettings},
    session::{self, AppPaths, RunSession, SavedRun},
    ui,
};
use chrono::Local;
use eframe::egui::{self, RichText};
use serde_json::{json, Value};
use std::{
    env, fs, io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, OnceLock,
    },
    time::{Duration, Instant},
};

const DEFAULT_HELP_CONTEXT_DELAY_MS: u64 = 300;
const DEFAULT_STAGE_DETECT_INTERVAL_MS: u64 = 5_000;
const DEFAULT_CONTEXT_MAX_TRANSCRIPT_CHARS: usize = 16_000;
const DEFAULT_CONTEXT_MAX_CHAT_CHARS: usize = 4_000;
const DEFAULT_CONTEXT_MAX_LIVE_PARTIAL_CHARS: usize = 1_200;
const DEFAULT_CONTEXT_MAX_COACH_MESSAGES: usize = 6;
const COMPACT_DEFAULT_WIDTH: f32 = 980.0;
const COMPACT_DEFAULT_HEIGHT: f32 = 300.0;
const COMPACT_MIN_WIDTH: f32 = 460.0;
const COMPACT_HORIZONTAL_MIN_HEIGHT: f32 = 300.0;
const COMPACT_VERTICAL_MIN_HEIGHT: f32 = 640.0;
const COMPACT_LAYOUT_BREAKPOINT_WIDTH: f32 = 720.0;
const COMPACT_MAX_HEIGHT: f32 = 720.0;
const COMPACT_HEADER_HEIGHT: f32 = 34.0;
const COMPACT_RESIZE_HANDLE_HEIGHT: f32 = 16.0;
const COMPACT_OVERLAY_EDGE_MARGIN_X: f32 = 0.0;
const COMPACT_OVERLAY_EDGE_MARGIN_Y: f32 = 0.0;
const COMPACT_OVERLAY_INNER_MARGIN: f32 = 10.0;
const COMPACT_OVERLAY_CHROME_HEIGHT: f32 = 72.0;
const COMPACT_VERTICAL_TRANSCRIPT_MIN_HEIGHT: f32 = 132.0;
const COMPACT_VERTICAL_INSTRUCTION_MIN_HEIGHT: f32 = 180.0;
const COMPACT_VERTICAL_HELP_MIN_HEIGHT: f32 = 132.0;
const DEFAULT_STAGE_UI_STATE_PATH: &str = "logs/rec-sidecar.stage-ui.json";

struct PendingHelpRequest {
    id: u64,
    run_id: String,
    due_at: Instant,
    created_at: Instant,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AsrLanguageSelection {
    Auto,
    Russian,
    English,
}

impl AsrLanguageSelection {
    const ALL: [Self; 3] = [Self::Auto, Self::Russian, Self::English];

    fn label(self) -> &'static str {
        match self {
            Self::Auto => "Auto",
            Self::Russian => "RU",
            Self::English => "EN",
        }
    }

    fn settings(self) -> asr::AsrSettings {
        let language = match self {
            Self::Auto => None,
            Self::Russian => Some(asr::AsrLanguagePreset::Russian),
            Self::English => Some(asr::AsrLanguagePreset::English),
        };

        asr::AsrSettings { language }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum WindowMode {
    Compact,
    Expanded,
}

pub struct RecApp {
    paths: AppPaths,
    recording: bool,
    connecting: bool,
    bubbles: Vec<String>,
    live_partial: Option<String>,
    coach_bubbles: Vec<String>,
    coach_live: Option<String>,
    coach_chat_input: String,
    coach_chat_messages: Vec<CoachChatMessage>,
    next_coach_chat_id: u64,
    pending_help: Option<PendingHelpRequest>,
    coach_tx: Option<Sender<coach::CoachInput>>,
    coach_rx: Option<Receiver<coach::CoachEvent>>,
    coach_status: String,
    stage_agenda: Option<coach::CoachStageAgenda>,
    stage_status: String,
    stage_export_sequence: u64,
    force_stage_detect: bool,
    last_stage_request_sent: Instant,
    force_coach_snapshot: bool,
    last_coach_snapshot_sent: Instant,
    rx: Option<Receiver<asr::AsrEvent>>,
    stop_flag: Option<Arc<AtomicBool>>,
    asr_cmd_tx: Option<Sender<asr::AsrCommand>>,
    asr_language_selection: AsrLanguageSelection,
    current_run: Option<RunSession>,
    history: Vec<SavedRun>,
    selected_history: Option<usize>,
    viewer_open: bool,
    viewer_title: String,
    viewer_text: String,
    status: String,
    auto_start_pending: bool,
    expanded_workspace: bool,
    applied_window_mode: Option<WindowMode>,
    compact_size: egui::Vec2,
    compact_position: Option<egui::Pos2>,
    #[cfg(test)]
    rendered_controls: Vec<String>,
    #[cfg(test)]
    rendered_affordances: Vec<String>,
}

impl Default for RecApp {
    fn default() -> Self {
        Self::new()
    }
}

impl RecApp {
    pub fn new() -> Self {
        Self::new_with_paths(AppPaths::default())
    }

    pub fn new_with_paths(paths: AppPaths) -> Self {
        let mut app = Self {
            paths,
            recording: false,
            connecting: false,
            bubbles: Vec::new(),
            live_partial: None,
            coach_bubbles: Vec::new(),
            coach_live: None,
            coach_chat_input: String::new(),
            coach_chat_messages: Vec::new(),
            next_coach_chat_id: 1,
            pending_help: None,
            coach_tx: None,
            coach_rx: None,
            coach_status: "Coach idle".to_string(),
            stage_agenda: None,
            stage_status: "Stage idle".to_string(),
            stage_export_sequence: 0,
            force_stage_detect: true,
            last_stage_request_sent: Instant::now(),
            force_coach_snapshot: false,
            last_coach_snapshot_sent: Instant::now(),
            rx: None,
            stop_flag: None,
            asr_cmd_tx: None,
            asr_language_selection: AsrLanguageSelection::Auto,
            current_run: None,
            history: Vec::new(),
            selected_history: None,
            viewer_open: false,
            viewer_title: String::new(),
            viewer_text: String::new(),
            status: "Ready".to_string(),
            auto_start_pending: env_flag("REC_AUTO_START"),
            expanded_workspace: !env_flag("REC_AUTO_START"),
            applied_window_mode: None,
            compact_size: egui::vec2(COMPACT_DEFAULT_WIDTH, COMPACT_DEFAULT_HEIGHT),
            compact_position: None,
            #[cfg(test)]
            rendered_controls: Vec::new(),
            #[cfg(test)]
            rendered_affordances: Vec::new(),
        };

        app.refresh_history();
        app
    }

    fn focus_live_workspace(&mut self) {
        // The live workspace is embedded in the main window now.
    }

    fn start_new_recording(&mut self) {
        self.stop_worker();
        self.stop_coach();
        self.bubbles.clear();
        self.live_partial = None;
        self.coach_bubbles.clear();
        self.coach_live = None;
        self.coach_chat_input.clear();
        self.coach_chat_messages.clear();
        self.next_coach_chat_id = 1;
        self.pending_help = None;
        self.stage_agenda = None;
        self.stage_status = "Stage detecting...".to_string();
        self.force_stage_detect = true;
        self.current_run = Some(RunSession::new());
        self.export_stage_ui_state();
        self.expanded_workspace = false;
        self.applied_window_mode = None;
        self.focus_live_workspace();
        self.spawn_coach();
        self.spawn_worker();
        self.status = "Connecting to Inworld STT...".to_string();
    }

    fn continue_recording(&mut self) {
        self.drain_asr();

        if self.current_run.is_none() {
            self.current_run = Some(RunSession::new());
        }

        self.focus_live_workspace();
        self.expanded_workspace = false;
        self.applied_window_mode = None;
        if self.coach_tx.is_none() {
            self.spawn_coach();
        }
        self.spawn_worker();
        self.status = "Connecting to Inworld STT...".to_string();
    }

    fn pause_recording(&mut self) {
        if !self.recording && !self.connecting {
            return;
        }

        self.stop_worker();
        self.status = "Recording stopped".to_string();
    }

    fn save_current_run(&mut self) -> io::Result<PathBuf> {
        self.drain_asr();
        self.drain_coach();

        if self.current_run.is_none() {
            self.current_run = Some(RunSession::new());
        }

        let content = self.render_transcript();
        let session = self.current_run.as_mut().expect("session exists");
        let path = session::save_run(&self.paths, session, &content)?;
        self.refresh_history();
        self.select_history_path(&path);
        self.status = format!("Saved {}", path.display());
        Ok(path)
    }

    fn save_and_exit(&mut self) {
        match self.save_current_run() {
            Ok(path) => {
                self.stop_worker();
                self.stop_coach();
                self.focus_live_workspace();
                self.current_run = None;
                self.bubbles.clear();
                self.live_partial = None;
                self.coach_bubbles.clear();
                self.coach_live = None;
                self.coach_chat_input.clear();
                self.coach_chat_messages.clear();
                self.pending_help = None;
                self.stage_agenda = None;
                self.stage_status = "Stage idle".to_string();
                self.force_stage_detect = true;
                self.export_stage_ui_state();
                self.expanded_workspace = true;
                self.applied_window_mode = None;
                self.rx = None;
                self.status = format!("Saved and closed {}", path.display());
            }
            Err(err) => {
                self.status = format!("Save failed: {}", err);
            }
        }
    }

    fn view_selected_history(&mut self) {
        let Some(run) = self.selected_run().cloned() else {
            self.status = "Select a historical run first".to_string();
            return;
        };

        match session::read_run_text(&run) {
            Ok(text) => {
                self.viewer_title = run.title;
                self.viewer_text = text;
                self.viewer_open = true;
                self.status = format!("Opened {}", run.path.display());
            }
            Err(err) => {
                self.status = format!("Open failed: {}", err);
            }
        }
    }

    fn export_selected_history(&mut self) {
        let Some(run) = self.selected_run().cloned() else {
            self.status = "Select a historical run first".to_string();
            return;
        };

        match self.export_run(&run) {
            Ok(path) => {
                self.status = format!("Exported {}", path.display());
            }
            Err(err) => {
                self.status = format!("Export failed: {}", err);
            }
        }
    }

    fn export_run(&self, run: &SavedRun) -> io::Result<PathBuf> {
        session::export_run(&self.paths, run)
    }

    fn spawn_worker(&mut self) {
        self.stop_worker();

        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));

        self.rx = Some(rx);
        self.stop_flag = Some(stop.clone());
        self.connecting = true;
        self.recording = false;

        self.asr_cmd_tx = asr::spawn_asr_worker(tx, stop, self.asr_language_selection.settings());
    }

    fn stop_worker(&mut self) {
        if let Some(stop) = &self.stop_flag {
            stop.store(true, Ordering::Relaxed);
        }

        self.stop_flag = None;
        self.asr_cmd_tx = None;
        self.connecting = false;
        self.recording = false;
    }

    fn spawn_coach(&mut self) {
        self.stop_coach();

        let (event_tx, event_rx) = mpsc::channel();
        if let Some(input_tx) = coach::spawn_coach_worker(event_tx, coach::CoachSettings) {
            self.coach_tx = Some(input_tx);
            self.coach_rx = Some(event_rx);
            self.coach_status = "Coach connecting...".to_string();
            self.force_coach_snapshot = true;
            self.force_stage_detect = true;
        } else {
            self.coach_tx = None;
            self.coach_rx = None;
            self.coach_status = "Coach disabled: missing provider config".to_string();
        }
    }

    fn stop_coach(&mut self) {
        if let Some(tx) = self.coach_tx.take() {
            let _ = tx.send(coach::CoachInput::Stop);
        }

        self.coach_rx = None;
        self.coach_status = "Coach idle".to_string();
        self.force_coach_snapshot = false;
        self.force_stage_detect = true;
        self.pending_help = None;
    }

    fn drain_asr(&mut self) {
        if let Some(rx) = &self.rx {
            let mut events = Vec::new();

            while let Ok(event) = rx.try_recv() {
                events.push(event);
            }

            for event in events {
                self.handle_asr_event(event);
            }
        }
    }

    fn drain_coach(&mut self) {
        if let Some(rx) = &self.coach_rx {
            let mut events = Vec::new();

            while let Ok(event) = rx.try_recv() {
                events.push(event);
            }

            for event in events {
                self.handle_coach_event(event);
            }
        }
    }

    fn handle_asr_event(&mut self, event: asr::AsrEvent) {
        match event {
            asr::AsrEvent::Connecting(message) => {
                self.connecting = true;
                self.recording = false;
                self.status = message;
            }
            asr::AsrEvent::Recovering(message) => {
                self.connecting = true;
                self.recording = false;
                self.live_partial = None;
                self.status = message;
            }
            asr::AsrEvent::Ready(message) => {
                self.connecting = false;
                self.recording = true;
                self.status = message;
            }
            asr::AsrEvent::PartialTranscript(text) => {
                self.live_partial = Some(text);
            }
            asr::AsrEvent::Transcript(text) => {
                self.live_partial = None;
                self.bubbles.push(text);
            }
            asr::AsrEvent::StageAgenda(agenda) => {
                self.apply_stage_agenda(*agenda);
            }
            asr::AsrEvent::StageError(message) => {
                self.stage_status = message;
            }
            asr::AsrEvent::Error(message) => {
                self.connecting = false;
                self.recording = false;
                self.status = message.clone();
                self.live_partial = None;
                self.bubbles.push(message);
            }
            asr::AsrEvent::Stopped => {
                self.connecting = false;
                self.recording = false;
            }
        }
    }

    fn handle_coach_event(&mut self, event: coach::CoachEvent) {
        match event {
            coach::CoachEvent::Ready(message) => {
                self.coach_status = message;
            }
            coach::CoachEvent::Started => {
                if let Some(text) = self.coach_live.take() {
                    if !text.trim().is_empty() {
                        self.coach_bubbles.push(text);
                    }
                }

                self.coach_live = Some(String::new());
                self.coach_status = "Coach streaming...".to_string();
            }
            coach::CoachEvent::Delta(text) => {
                self.coach_live
                    .get_or_insert_with(String::new)
                    .push_str(&text);
            }
            coach::CoachEvent::Finished => {
                let mut pushed = false;
                if let Some(text) = self.coach_live.take() {
                    if !text.trim().is_empty() {
                        self.coach_bubbles.push(text.trim().to_string());
                        pushed = true;
                    }
                }

                self.coach_status = "Coach ready".to_string();
                self.force_coach_snapshot = pushed;
            }
            coach::CoachEvent::StageAgenda(agenda) => {
                self.apply_stage_agenda(*agenda);
            }
            coach::CoachEvent::StageError(message) => {
                self.stage_status = message;
            }
            coach::CoachEvent::HelpStage(id, stage) => {
                self.set_help_stage(id, stage);
            }
            coach::CoachEvent::ChatModel(id, model) => {
                self.add_chat_model_label(id, model);
            }
            coach::CoachEvent::Error(message) => {
                self.coach_status = message.clone();
                self.status = message;
            }
            coach::CoachEvent::ChatStarted(id) => {
                if let Some(message) = self.chat_message_mut(id) {
                    message.streaming = true;
                }
            }
            coach::CoachEvent::ChatDelta(id, text) => {
                if let Some(message) = self.chat_message_mut(id) {
                    message.text.push_str(&text);
                    message.streaming = true;
                }
            }
            coach::CoachEvent::ChatFinished(id) => {
                if let Some(message) = self.chat_message_mut(id) {
                    message.streaming = false;
                    message.help_stage_label = None;
                    if message.text.trim().is_empty() {
                        message.text = "Ответ пустой.".to_string();
                    }
                }
            }
            coach::CoachEvent::ChatError(id, message) => {
                if let Some(chat_message) = self.chat_message_mut(id) {
                    chat_message.text = message.clone();
                    chat_message.streaming = false;
                    chat_message.help_stage_label = Some(coach::CoachHelpStage::Error.label());
                }
                self.coach_status = coach::CoachHelpStage::Error.status();
                self.status = message;
            }
            coach::CoachEvent::Stopped => {
                self.coach_status = "Coach stopped".to_string();
            }
        }
    }

    fn apply_stage_agenda(&mut self, agenda: coach::CoachStageAgenda) {
        if let Some(current) = self.stage_agenda.as_ref() {
            if stage_is_backward(&current.stage, &agenda.stage) {
                coach::log_event(format!(
                    "stage backward ignored current_stage={} incoming_stage={} model={}",
                    current.stage, agenda.stage, agenda.model
                ));
                self.stage_status = format!(
                    "{} · удерживаю, откат {} игнорирован · {}",
                    current.stage, agenda.stage, agenda.model
                );
                self.force_stage_detect = false;
                return;
            }
        }

        self.stage_status = if let Some(scorecard) = &agenda.scorecard {
            format!(
                "{} · {} · {}",
                agenda.stage, scorecard.readiness_label, agenda.model
            )
        } else {
            format!("{} · {}", agenda.stage, agenda.model)
        };
        self.stage_agenda = Some(agenda);
        self.force_stage_detect = false;
        self.export_stage_ui_state();
    }

    fn export_stage_ui_state(&mut self) {
        self.stage_export_sequence = self.stage_export_sequence.saturating_add(1);
        let path = stage_ui_state_path();
        let value = self.stage_ui_state_value();
        if let Err(err) = write_json_atomic(&path, &value) {
            coach::log_event(format!(
                "stage ui state export failed path={} error={}",
                path.display(),
                err
            ));
        }
    }

    fn stage_ui_state_value(&self) -> Value {
        let run_id = self
            .current_run
            .as_ref()
            .map(|run| run.id.as_str())
            .unwrap_or("");

        if let Some(agenda) = &self.stage_agenda {
            let scorecard = agenda.scorecard.as_ref();
            let advice = scorecard
                .map(|scorecard| speakable_stage_action(&scorecard.next_action))
                .unwrap_or_else(|| speakable_stage_action(&agenda.step));
            let visible_label = scorecard
                .map(|scorecard| {
                    format!(
                        "{} · {} · {} · {}/{}",
                        agenda.stage,
                        agenda.title,
                        scorecard.readiness_label,
                        scorecard.hit_count,
                        scorecard.total_count
                    )
                })
                .unwrap_or_else(|| format!("{} · {}", agenda.stage, agenda.title));
            let checks = scorecard
                .map(|scorecard| {
                    scorecard
                        .checks
                        .iter()
                        .map(|check| {
                            json!({
                                "id": check.id,
                                "label": check.label,
                                "level": check.level,
                                "result": check.result,
                                "signal": check.signal,
                                "reason": check.reason,
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let signals = scorecard
                .map(|scorecard| {
                    scorecard
                        .signals
                        .iter()
                        .map(|signal| {
                            json!({
                                "id": signal.id,
                                "label": signal.label,
                                "state": signal.state,
                                "detail": signal.detail,
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();

            json!({
                "source": "rust-ui",
                "sequence": self.stage_export_sequence,
                "updated_at_ms": Local::now().timestamp_millis(),
                "run_id": run_id,
                "has_stage": true,
                "status": self.stage_status,
                "stage": agenda.stage,
                "title": agenda.title,
                "agenda": agenda.agenda,
                "emotion": agenda.emotion,
                "step": agenda.step,
                "provider": agenda.provider,
                "model": agenda.model,
                "visible_label": visible_label,
                "speakable_next_action": advice,
                "scorecard": {
                    "readiness": scorecard.map(|scorecard| scorecard.readiness.as_str()).unwrap_or(""),
                    "readiness_label": scorecard.map(|scorecard| scorecard.readiness_label.as_str()).unwrap_or(""),
                    "score": scorecard.and_then(|scorecard| scorecard.score),
                    "hit_count": scorecard.map(|scorecard| scorecard.hit_count).unwrap_or(0),
                    "miss_count": scorecard.map(|scorecard| scorecard.miss_count).unwrap_or(0),
                    "total_count": scorecard.map(|scorecard| scorecard.total_count).unwrap_or(0),
                    "hard_red": scorecard.map(|scorecard| scorecard.hard_red).unwrap_or(false),
                    "ready_to_advance": scorecard.map(|scorecard| scorecard.ready_to_advance).unwrap_or(false),
                    "next_action": scorecard.map(|scorecard| scorecard.next_action.as_str()).unwrap_or(""),
                    "summary": scorecard.map(|scorecard| scorecard.summary.as_str()).unwrap_or(""),
                    "checks": checks,
                    "signals": signals,
                }
            })
        } else {
            json!({
                "source": "rust-ui",
                "sequence": self.stage_export_sequence,
                "updated_at_ms": Local::now().timestamp_millis(),
                "run_id": run_id,
                "has_stage": false,
                "status": self.stage_status,
                "stage": null,
                "title": null,
                "visible_label": self.stage_status,
                "speakable_next_action": "",
                "scorecard": null,
            })
        }
    }

    fn chat_message_mut(&mut self, request_id: u64) -> Option<&mut CoachChatMessage> {
        self.coach_chat_messages
            .iter_mut()
            .find(|message| message.request_id == Some(request_id))
    }

    fn add_chat_model_label(&mut self, request_id: u64, model: String) {
        let model = model.trim();
        if model.is_empty() {
            return;
        }

        if let Some(message) = self.chat_message_mut(request_id) {
            match &mut message.model_label {
                Some(label) if label.split(" + ").any(|part| part == model) => {}
                Some(label) => {
                    label.push_str(" + ");
                    label.push_str(model);
                }
                None => {
                    message.model_label = Some(model.to_string());
                }
            }
        }
    }

    fn set_help_stage(&mut self, request_id: u64, stage: coach::CoachHelpStage) {
        if let Some(message) = self.chat_message_mut(request_id) {
            message.help_stage_label = Some(stage.label());
        }

        let status = stage.status();
        self.coach_status = status.clone();
        self.status = status;
    }

    fn help_is_busy(&self) -> bool {
        self.pending_help.is_some() || self.chat_is_streaming()
    }

    fn send_help_request(&mut self) {
        self.drain_asr();
        self.drain_coach();

        if self.help_is_busy() {
            self.status = "Coach is already preparing help".to_string();
            self.focus_live_workspace();
            return;
        }

        if self.current_run.is_none() {
            self.status = "Start a run before asking coach for help".to_string();
            self.focus_live_workspace();
            return;
        }

        if self.coach_tx.is_none() {
            self.spawn_coach();
        }

        if self.coach_tx.is_none() {
            self.status = "Coach disabled: missing provider config".to_string();
            self.focus_live_workspace();
            return;
        }

        let Some(run_id) = self.current_run.as_ref().map(|run| run.id.clone()) else {
            return;
        };

        let request_id = self.next_coach_chat_id;
        self.next_coach_chat_id += 1;
        coach::log_event(format!(
            "help button clicked id={} run_id={}",
            request_id, run_id
        ));

        self.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::User,
            text: "Помоги".to_string(),
            request_id: None,
            streaming: false,
            help_stage_label: None,
            model_label: None,
        });
        self.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::Assistant,
            text: String::new(),
            request_id: Some(request_id),
            streaming: true,
            help_stage_label: Some(coach::CoachHelpStage::FreezingContext.label()),
            model_label: None,
        });
        self.set_help_stage(request_id, coach::CoachHelpStage::FreezingContext);
        self.focus_live_workspace();

        let asr_active = self.recording || self.connecting;
        if asr_active {
            if let Some(asr_tx) = self.asr_cmd_tx.clone() {
                match asr_tx.send(asr::AsrCommand::Flush {
                    reason: "help".to_string(),
                }) {
                    Ok(()) => {
                        let now = Instant::now();
                        self.pending_help = Some(PendingHelpRequest {
                            id: request_id,
                            run_id,
                            due_at: now + help_context_delay(),
                            created_at: now,
                        });
                        coach::log_event(format!("ASR flush sent id={} reason=help", request_id));
                        return;
                    }
                    Err(err) => {
                        coach::log_event(format!(
                            "ASR flush send failed id={} error={}",
                            request_id, err
                        ));
                    }
                }
            }
        }

        self.dispatch_help_request(request_id, run_id);
    }

    fn maybe_dispatch_pending_help(&mut self) {
        let should_dispatch = self
            .pending_help
            .as_ref()
            .map(|pending| Instant::now() >= pending.due_at)
            .unwrap_or(false);

        if !should_dispatch {
            return;
        }

        let Some(pending) = self.pending_help.take() else {
            return;
        };

        self.drain_asr();
        coach::log_event(format!(
            "help context freeze delay elapsed_ms={} id={}",
            pending.created_at.elapsed().as_millis(),
            pending.id
        ));
        self.dispatch_help_request(pending.id, pending.run_id);
    }

    fn dispatch_help_request(&mut self, request_id: u64, run_id: String) {
        if self.coach_tx.is_none() {
            self.spawn_coach();
        }

        let Some(tx) = self.coach_tx.clone() else {
            self.status = "Coach disabled: missing provider config".to_string();
            if let Some(message) = self.chat_message_mut(request_id) {
                message.text = "Coach disabled: missing provider config.".to_string();
                message.streaming = false;
                message.help_stage_label = Some(coach::CoachHelpStage::Error.label());
            }
            return;
        };

        let context = self.render_help_context();
        coach::log_event(format!(
            "help context chars id={} chars={}",
            request_id,
            context.chars().count()
        ));

        let request = coach::CoachHelpRequest {
            id: request_id,
            run_id,
            context,
        };

        if tx.send(coach::CoachInput::Help(request)).is_err() {
            if let Some(message) = self.chat_message_mut(request_id) {
                message.text = "Coach worker is not available.".to_string();
                message.streaming = false;
                message.help_stage_label = Some(coach::CoachHelpStage::Error.label());
            }
            self.set_help_stage(request_id, coach::CoachHelpStage::Error);
        } else {
            self.set_help_stage(request_id, coach::CoachHelpStage::PreparingOpener);
        }
    }

    fn send_typed_chat_question(&mut self) {
        let question = self.coach_chat_input.trim().to_string();
        if question.is_empty() {
            return;
        }

        self.coach_chat_input.clear();
        self.send_coach_chat_question(question);
    }

    fn send_coach_chat_question(&mut self, question: impl Into<String>) {
        self.drain_asr();
        self.drain_coach();

        let question = question.into().trim().to_string();
        if question.is_empty() {
            return;
        }

        if self.current_run.is_none() {
            self.status = "Start a run before chatting with coach".to_string();
            self.focus_live_workspace();
            return;
        }

        if self.coach_tx.is_none() {
            self.spawn_coach();
        }

        let Some(tx) = self.coach_tx.clone() else {
            self.status = "Coach disabled: missing provider config".to_string();
            self.focus_live_workspace();
            return;
        };

        let Some(run_id) = self.current_run.as_ref().map(|run| run.id.clone()) else {
            return;
        };

        let context = self.render_chat_context();
        let request_id = self.next_coach_chat_id;
        self.next_coach_chat_id += 1;

        self.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::User,
            text: question.clone(),
            request_id: None,
            streaming: false,
            help_stage_label: None,
            model_label: None,
        });
        self.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::Assistant,
            text: String::new(),
            request_id: Some(request_id),
            streaming: true,
            help_stage_label: None,
            model_label: None,
        });
        self.focus_live_workspace();

        let request = coach::CoachChatRequest {
            id: request_id,
            run_id,
            question,
            context,
        };

        if tx.send(coach::CoachInput::Chat(request)).is_err() {
            if let Some(message) = self.chat_message_mut(request_id) {
                message.text = "Coach worker is not available.".to_string();
                message.streaming = false;
            }
        }
    }

    fn maybe_send_coach_snapshot(&mut self) {
        if self.coach_tx.is_none() || self.current_run.is_none() {
            return;
        }

        if !self.force_coach_snapshot
            && self.last_coach_snapshot_sent.elapsed() < Duration::from_millis(250)
        {
            return;
        }

        if self.bubbles.is_empty() && self.live_partial.is_none() {
            return;
        }

        let Some(run_id) = self.current_run.as_ref().map(|run| run.id.clone()) else {
            return;
        };
        let Some(tx) = self.coach_tx.clone() else {
            return;
        };

        let content = self.render_coach_context();
        let snapshot = coach::CoachSnapshot {
            run_id,
            content,
            current_text: self.current_coach_reply_text(),
            force: self.force_coach_snapshot,
        };

        if tx.send(coach::CoachInput::Snapshot(snapshot)).is_ok() {
            self.force_coach_snapshot = false;
            self.last_coach_snapshot_sent = Instant::now();
        }
    }

    fn maybe_send_stage_request(&mut self) {
        if stage_audio_live_enabled() && !stage_rest_fallback_enabled() {
            return;
        }
        if self.coach_tx.is_none() || self.current_run.is_none() {
            return;
        }

        let interval = stage_detect_interval();
        if !self.force_stage_detect && self.last_stage_request_sent.elapsed() < interval {
            return;
        }

        if self.bubbles.is_empty() && self.live_partial.is_none() {
            return;
        }

        let Some(run_id) = self.current_run.as_ref().map(|run| run.id.clone()) else {
            return;
        };
        let Some(tx) = self.coach_tx.clone() else {
            return;
        };

        let request = coach::CoachStageRequest {
            run_id,
            context: self.render_coach_context(),
            current_stage: self
                .stage_agenda
                .as_ref()
                .map(|agenda| agenda.stage.clone()),
        };

        if tx.send(coach::CoachInput::Stage(request)).is_ok() {
            self.force_stage_detect = false;
            self.last_stage_request_sent = Instant::now();
            if self.stage_agenda.is_none() {
                self.stage_status = "Stage detecting...".to_string();
            }
        }
    }

    fn chat_is_streaming(&self) -> bool {
        self.coach_chat_messages
            .iter()
            .any(|message| message.streaming)
    }

    fn refresh_history(&mut self) {
        let previous = self.selected_run().map(|run| run.path.clone());
        self.history = session::read_saved_runs(&self.paths);

        self.selected_history = previous
            .as_ref()
            .and_then(|path| self.history.iter().position(|run| &run.path == path))
            .or_else(|| (!self.history.is_empty()).then_some(0));
    }

    fn select_history_path(&mut self, path: &Path) {
        self.selected_history = self.history.iter().position(|run| run.path == path);
    }

    fn selected_run(&self) -> Option<&SavedRun> {
        self.selected_history
            .and_then(|index| self.history.get(index))
    }

    fn context_input(&self) -> ContextInput<'_> {
        ContextInput {
            transcript: &self.bubbles,
            live_partial: self.live_partial.as_deref(),
            coach_bubbles: &self.coach_bubbles,
            coach_live: self.coach_live.as_deref(),
            coach_chat_messages: &self.coach_chat_messages,
            stage_agenda: self
                .stage_agenda
                .as_ref()
                .map(|agenda| context::StageAgendaContext {
                    stage: agenda.stage.as_str(),
                    title: agenda.title.as_str(),
                    agenda: agenda.agenda.as_str(),
                    emotion: agenda.emotion.as_str(),
                    step: agenda.step.as_str(),
                }),
        }
    }

    fn current_coach_reply_text(&self) -> Option<String> {
        if let Some(text) = self
            .coach_live
            .as_deref()
            .map(str::trim)
            .filter(|text| !text.is_empty() && !is_technical_coach_status(text))
        {
            return Some(text.to_string());
        }

        self.coach_bubbles
            .iter()
            .rev()
            .map(String::as_str)
            .map(str::trim)
            .find(|text| !text.is_empty() && !is_technical_coach_status(text))
            .map(ToString::to_string)
    }

    fn render_transcript(&self) -> String {
        let now = Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
        let title = self
            .current_run
            .as_ref()
            .map(|run| run.title.as_str())
            .unwrap_or("Untitled run");

        context::render_transcript(title, &now, &self.context_input())
    }

    fn render_coach_context(&self) -> String {
        context::render_coach_context(&self.context_input())
    }

    fn render_chat_context(&self) -> String {
        context::render_chat_context(&self.context_input())
    }

    fn render_help_context(&self) -> String {
        context::render_help_context(
            &self.context_input(),
            HelpContextSettings {
                max_transcript_chars: env_usize(
                    "COACH_CONTEXT_MAX_TRANSCRIPT_CHARS",
                    DEFAULT_CONTEXT_MAX_TRANSCRIPT_CHARS,
                ),
                max_chat_chars: env_usize(
                    "COACH_CONTEXT_MAX_CHAT_CHARS",
                    DEFAULT_CONTEXT_MAX_CHAT_CHARS,
                ),
                max_live_partial_chars: env_usize(
                    "COACH_CONTEXT_MAX_LIVE_PARTIAL_CHARS",
                    DEFAULT_CONTEXT_MAX_LIVE_PARTIAL_CHARS,
                ),
                max_coach_messages: env_usize(
                    "COACH_CONTEXT_MAX_COACH_MESSAGES",
                    DEFAULT_CONTEXT_MAX_COACH_MESSAGES,
                ),
            },
        )
    }

    fn control_button(&mut self, ui: &mut egui::Ui, label: &str) -> egui::Response {
        self.record_rendered_control(label);
        ui.button(label)
    }

    fn control_button_enabled(
        &mut self,
        ui: &mut egui::Ui,
        enabled: bool,
        label: &str,
    ) -> egui::Response {
        self.record_rendered_control(label);
        ui.add_enabled(enabled, egui::Button::new(label))
    }

    fn display_control_button(
        &mut self,
        ui: &mut egui::Ui,
        enabled: bool,
        record_label: &str,
        display_label: &str,
    ) -> egui::Response {
        self.record_rendered_control(record_label);
        let mut button = egui::Button::new(RichText::new(display_label).size(11.5))
            .fill(egui::Color32::from_rgba_unmultiplied(255, 255, 255, 19))
            .stroke(egui::Stroke::new(
                1.0,
                egui::Color32::from_rgba_unmultiplied(255, 255, 255, 34),
            ));
        if record_label == "Помоги" {
            button = button.fill(egui::Color32::from_rgba_unmultiplied(67, 209, 125, 34));
        }
        ui.add_enabled(enabled, button)
    }

    #[cfg(test)]
    fn record_rendered_control(&mut self, label: &str) {
        self.rendered_controls.push(label.to_string());
    }

    #[cfg(not(test))]
    fn record_rendered_control(&mut self, _label: &str) {}

    #[cfg(test)]
    fn record_rendered_affordance(&mut self, label: &str) {
        self.rendered_affordances.push(label.to_string());
    }

    #[cfg(not(test))]
    fn record_rendered_affordance(&mut self, _label: &str) {}

    fn show_main_window(&mut self, ctx: &egui::Context) {
        ui::apply_liquid_glass_style(ctx);
        let window_mode = if self.current_run.is_some() && !self.expanded_workspace {
            WindowMode::Compact
        } else {
            WindowMode::Expanded
        };
        self.apply_window_mode(ctx, window_mode);

        if window_mode == WindowMode::Compact {
            egui::CentralPanel::default()
                .frame(ui::transparent_frame())
                .show(ctx, |ui| {
                    self.show_compact_overlay(ctx, ui);
                });
            return;
        }

        egui::TopBottomPanel::top("top_bar")
            .frame(ui::toolbar_frame())
            .show(ctx, |ui| {
                self.show_top_bar(ctx, ui);
            });

        if self.current_run.is_some() {
            egui::SidePanel::left("transcript_dock")
                .resizable(true)
                .default_width(300.0)
                .width_range(250.0..=420.0)
                .frame(ui::app_background_frame())
                .show(ctx, |ui| {
                    ui::glass_panel_frame().show(ui, |ui| {
                        ui::section_header(ui, "Расшифровка", self.transcript_state_label());
                        ui.separator();
                        self.show_transcript_column(ui);
                    });
                });

            egui::SidePanel::right("coach_dock")
                .resizable(true)
                .default_width(360.0)
                .width_range(300.0..=480.0)
                .frame(ui::app_background_frame())
                .show(ctx, |ui| {
                    ui::glass_panel_frame().show(ui, |ui| {
                        ui::section_header(ui, "Тренер", self.coach_state_label());
                        ui.separator();
                        self.show_coach_column(ui);
                    });
                });

            egui::CentralPanel::default()
                .frame(ui::app_background_frame())
                .show(ctx, |ui| {
                    self.show_live_center(ui);
                });
        } else {
            egui::CentralPanel::default()
                .frame(ui::app_background_frame())
                .show(ctx, |ui| {
                    self.show_idle_dashboard(ctx, ui);
                });
        }
    }

    fn compact_size_for_monitor(&self, monitor: egui::Vec2) -> egui::Vec2 {
        let max_width = (monitor.x - 40.0).max(COMPACT_MIN_WIDTH);
        let max_height = self.compact_max_height(monitor);
        let width = self.compact_size.x.clamp(COMPACT_MIN_WIDTH, max_width);
        let min_height = Self::compact_min_height_for_width(width);
        egui::vec2(width, self.compact_size.y.clamp(min_height, max_height))
    }

    fn compact_max_height(&self, monitor: egui::Vec2) -> f32 {
        COMPACT_MAX_HEIGHT.min(
            (monitor.y - 120.0)
                .max(COMPACT_HORIZONTAL_MIN_HEIGHT)
                .max(COMPACT_VERTICAL_MIN_HEIGHT),
        )
    }

    fn compact_min_height_for_width(width: f32) -> f32 {
        if width < COMPACT_LAYOUT_BREAKPOINT_WIDTH {
            COMPACT_VERTICAL_MIN_HEIGHT
        } else {
            COMPACT_HORIZONTAL_MIN_HEIGHT
        }
    }

    fn compact_is_vertical(&self) -> bool {
        self.compact_size.x < COMPACT_LAYOUT_BREAKPOINT_WIDTH
    }

    fn compact_default_position(&self, monitor: egui::Vec2, size: egui::Vec2) -> egui::Pos2 {
        egui::pos2(((monitor.x - size.x) / 2.0).max(18.0), 56.0)
    }

    fn monitor_size(ctx: &egui::Context) -> egui::Vec2 {
        ctx.input(|input| {
            input
                .viewport()
                .monitor_size
                .unwrap_or_else(|| egui::vec2(1440.0, 900.0))
        })
    }

    fn remember_compact_geometry(&mut self, ctx: &egui::Context) {
        let viewport = ctx.input(|input| input.viewport().clone());
        let monitor = viewport
            .monitor_size
            .unwrap_or_else(|| egui::vec2(1440.0, 900.0));
        if let Some(inner_rect) = viewport.inner_rect {
            if inner_rect.width() > 0.0 && inner_rect.height() > 0.0 {
                self.compact_size = egui::vec2(
                    inner_rect
                        .width()
                        .clamp(COMPACT_MIN_WIDTH, monitor.x.max(COMPACT_MIN_WIDTH)),
                    inner_rect.height().clamp(
                        Self::compact_min_height_for_width(inner_rect.width()),
                        self.compact_max_height(monitor),
                    ),
                );
            }
        }
        if let Some(outer_rect) = viewport.outer_rect {
            self.compact_position = Some(outer_rect.min);
        }
    }

    #[cfg(test)]
    fn resize_compact_overlay(&mut self, ctx: &egui::Context, requested_size: egui::Vec2) {
        let monitor = Self::monitor_size(ctx);
        self.compact_size = requested_size;
        let size = self.compact_size_for_monitor(monitor);
        self.compact_size = size;
        ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(size));
        ctx.request_repaint();
    }

    fn apply_window_mode(&mut self, ctx: &egui::Context, mode: WindowMode) {
        if self.applied_window_mode == Some(mode) {
            return;
        }

        let monitor = Self::monitor_size(ctx);
        let (size, min_size, position, window_level) = match mode {
            WindowMode::Compact => {
                let size = self.compact_size_for_monitor(monitor);
                self.compact_size = size;
                let position = self
                    .compact_position
                    .unwrap_or_else(|| self.compact_default_position(monitor, size));
                (
                    size,
                    egui::vec2(
                        COMPACT_MIN_WIDTH,
                        Self::compact_min_height_for_width(size.x),
                    ),
                    position,
                    egui::WindowLevel::AlwaysOnTop,
                )
            }
            WindowMode::Expanded => (
                egui::vec2(1320.0_f32.min(monitor.x - 80.0).max(960.0), 820.0),
                egui::vec2(960.0, 620.0),
                egui::pos2(((monitor.x - 1320.0) / 2.0).max(48.0), 82.0),
                egui::WindowLevel::Normal,
            ),
        };

        ctx.send_viewport_cmd(egui::ViewportCommand::Transparent(true));
        ctx.send_viewport_cmd(egui::ViewportCommand::Decorations(false));
        ctx.send_viewport_cmd(egui::ViewportCommand::Resizable(true));
        ctx.send_viewport_cmd(egui::ViewportCommand::WindowLevel(window_level));
        ctx.send_viewport_cmd(egui::ViewportCommand::MinInnerSize(min_size));
        ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(size));
        ctx.send_viewport_cmd(egui::ViewportCommand::OuterPosition(position));
        self.applied_window_mode = Some(mode);
    }

    fn show_compact_overlay(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        self.remember_compact_geometry(ctx);
        self.enforce_compact_viewport_bounds(ctx);
        let panel_min_height = (self.compact_size.y - COMPACT_OVERLAY_CHROME_HEIGHT).max(
            Self::compact_min_height_for_width(self.compact_size.x) - COMPACT_OVERLAY_CHROME_HEIGHT,
        );
        let available_rect = ui.available_rect_before_wrap();
        let root_rect = available_rect.shrink2(egui::vec2(
            COMPACT_OVERLAY_EDGE_MARGIN_X,
            COMPACT_OVERLAY_EDGE_MARGIN_Y,
        ));
        let content_rect = root_rect.shrink(COMPACT_OVERLAY_INNER_MARGIN);
        ui.allocate_rect(root_rect, egui::Sense::hover());
        ui::paint_compact_overlay_background(ui, root_rect);

        let mut overlay_ui = ui.new_child(
            egui::UiBuilder::new()
                .id_salt("compact_overlay_content")
                .max_rect(content_rect)
                .layout(egui::Layout::top_down(egui::Align::Min)),
        );
        overlay_ui.set_width(content_rect.width());
        overlay_ui.set_min_height(panel_min_height);
        self.show_compact_header(ctx, &mut overlay_ui);
        overlay_ui.add_space(6.0);
        self.show_compact_workflow(&mut overlay_ui);
        self.show_compact_resize_affordances(ui, available_rect);
    }

    fn enforce_compact_viewport_bounds(&mut self, ctx: &egui::Context) {
        let viewport = ctx.input(|input| input.viewport().clone());
        let monitor = viewport
            .monitor_size
            .unwrap_or_else(|| egui::vec2(1440.0, 900.0));
        let size = self.compact_size_for_monitor(monitor);
        self.compact_size = size;
        let min_size = egui::vec2(
            COMPACT_MIN_WIDTH,
            Self::compact_min_height_for_width(size.x),
        );
        ctx.send_viewport_cmd(egui::ViewportCommand::MinInnerSize(min_size));

        if let Some(inner_rect) = viewport.inner_rect {
            let current = inner_rect.size();
            if current.x + 0.5 < size.x || current.y + 0.5 < size.y {
                ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(size));
            }
        }
    }

    fn show_compact_header(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        self.record_rendered_affordance("compact-drag-header");
        let app_state = self.app_state_label();
        let app_state_color = self.app_state_color();
        let stage_label = self.compact_stage_label();
        let pause_label = if self.connecting || self.recording {
            "Стоп"
        } else {
            "Продолжить"
        };
        let help_busy = self.help_is_busy();

        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            ui.spacing_mut().button_padding = egui::vec2(8.0, 5.0);
            let reserved_controls_width = if pause_label == "Продолжить" {
                500.0
            } else {
                455.0
            };
            let drag_width = if ui.available_width() < COMPACT_LAYOUT_BREAKPOINT_WIDTH {
                ui.available_width()
            } else {
                (ui.available_width() - reserved_controls_width)
                    .clamp(150.0, ui.available_width().max(150.0))
            };
            let (drag_rect, drag_response) = ui.allocate_exact_size(
                egui::vec2(drag_width, COMPACT_HEADER_HEIGHT),
                egui::Sense::click_and_drag(),
            );
            let drag_response = drag_response.on_hover_and_drag_cursor(egui::CursorIcon::Grab);
            if drag_response.drag_started_by(egui::PointerButton::Primary) {
                self.remember_compact_geometry(ctx);
                ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
            }

            let painter = ui.painter();
            let grab_color = egui::Color32::from_rgba_unmultiplied(235, 242, 248, 120);
            let text_color = egui::Color32::from_rgb(235, 242, 248);
            for row in 0..2 {
                for col in 0..3 {
                    let center = egui::pos2(
                        drag_rect.left() + 8.0 + col as f32 * 6.0,
                        drag_rect.center().y - 3.5 + row as f32 * 7.0,
                    );
                    painter.circle_filled(center, 1.5, grab_color);
                }
            }
            painter.text(
                egui::pos2(drag_rect.left() + 30.0, drag_rect.center().y - 6.0),
                egui::Align2::LEFT_CENTER,
                "REC Sidecar",
                egui::FontId::proportional(13.0),
                text_color,
            );
            painter.text(
                egui::pos2(drag_rect.left() + 30.0, drag_rect.center().y + 9.0),
                egui::Align2::LEFT_CENTER,
                format!("{} · {}", app_state, stage_label),
                egui::FontId::proportional(11.0),
                app_state_color,
            );

            if self
                .display_control_button(ui, true, pause_label, pause_label)
                .clicked()
            {
                if self.connecting || self.recording {
                    self.pause_recording();
                } else {
                    self.continue_recording();
                }
            }

            if self
                .display_control_button(ui, !help_busy, "Помоги", "Помоги")
                .on_disabled_hover_text("Тренер уже готовит подсказку")
                .clicked()
            {
                self.send_help_request();
            }

            if self
                .display_control_button(ui, self.current_run.is_some(), "Сохранить", "Сохр.")
                .on_hover_text("Сохранить запись")
                .clicked()
            {
                if let Err(err) = self.save_current_run() {
                    self.status = format!("Save failed: {}", err);
                }
            }

            if self
                .display_control_button(
                    ui,
                    self.current_run.is_some(),
                    "Сохр. и закрыть",
                    "Сохр.+закр.",
                )
                .on_hover_text("Сохранить и закрыть запись")
                .clicked()
            {
                self.save_and_exit();
            }

            if self
                .display_control_button(ui, true, "Развернуть", "Разв.")
                .on_hover_text("Развернуть рабочее окно")
                .clicked()
            {
                self.remember_compact_geometry(ctx);
                self.expanded_workspace = true;
                self.applied_window_mode = None;
            }

            if self
                .display_control_button(ui, true, "Выйти", "Выйти")
                .clicked()
            {
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        });
    }

    fn compact_stage_label(&self) -> String {
        self.stage_agenda
            .as_ref()
            .map(|agenda| agenda.stage.clone())
            .unwrap_or_else(|| "stage pending".to_string())
    }

    fn show_compact_workflow(&mut self, ui: &mut egui::Ui) {
        let available_height =
            (ui.available_height() - COMPACT_RESIZE_HANDLE_HEIGHT - 8.0).max(0.0);
        if self.compact_is_vertical() {
            self.record_rendered_affordance("compact-layout-vertical");
            let section_gap = 8.0;
            let base_height = COMPACT_VERTICAL_TRANSCRIPT_MIN_HEIGHT
                + COMPACT_VERTICAL_INSTRUCTION_MIN_HEIGHT
                + COMPACT_VERTICAL_HELP_MIN_HEIGHT;
            let content_height = (available_height - section_gap * 2.0).max(base_height);
            let extra_height = content_height - base_height;
            let transcript_height = COMPACT_VERTICAL_TRANSCRIPT_MIN_HEIGHT + extra_height * 0.22;
            let instruction_height = COMPACT_VERTICAL_INSTRUCTION_MIN_HEIGHT + extra_height * 0.48;
            let help_height = COMPACT_VERTICAL_HELP_MIN_HEIGHT + extra_height * 0.30;

            ui.vertical(|ui| {
                self.show_compact_transcript_panel(ui, ui.available_width(), transcript_height);
                compact_section_separator(ui, section_gap);
                self.show_compact_instruction_panel(ui, ui.available_width(), instruction_height);
                compact_section_separator(ui, section_gap);
                self.show_compact_help_chat_panel(ui, ui.available_width(), help_height);
            });
        } else {
            self.record_rendered_affordance("compact-layout-horizontal");
            let content_height = available_height.max(170.0);
            let total_width = ui.available_width();
            let gap_width = 12.0;
            let transcript_width = (total_width * 0.24).clamp(190.0, 270.0);
            let help_width = (total_width * 0.22).clamp(170.0, 250.0);
            let instruction_width =
                (total_width - transcript_width - help_width - gap_width * 2.0).max(0.0);

            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 0.0;
                self.show_compact_transcript_panel(ui, transcript_width, content_height);
                compact_vertical_separator(ui, gap_width, content_height);
                self.show_compact_instruction_panel(ui, instruction_width, content_height);
                compact_vertical_separator(ui, gap_width, content_height);
                self.show_compact_help_chat_panel(ui, help_width, content_height);
            });
        }
    }

    fn show_compact_transcript_panel(&mut self, ui: &mut egui::Ui, width: f32, height: f32) {
        self.record_rendered_affordance("compact-transcript-panel");
        ui.allocate_ui_with_layout(
            egui::vec2(width, height),
            egui::Layout::top_down(egui::Align::Min),
            |ui| {
                ui::compact_panel_frame().show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 4.0;
                    ui.set_width(ui.available_width());
                    ui.set_min_height(compact_panel_inner_height(height));
                    ui.label(RichText::new("Транскрипт").size(12.0).strong());
                    ui.label(
                        RichText::new(self.transcript_state_label())
                            .size(10.5)
                            .color(egui::Color32::from_rgb(168, 180, 192)),
                    );
                    ui.separator();
                    let last_line = self
                        .live_partial
                        .as_deref()
                        .or_else(|| self.bubbles.last().map(String::as_str))
                        .unwrap_or("Жду речь...");
                    egui::ScrollArea::vertical()
                        .id_salt("compact_transcript_scroll")
                        .auto_shrink([false, false])
                        .max_height((height - 96.0).max(36.0))
                        .show(ui, |ui| {
                            ui.add(egui::Label::new(RichText::new(last_line).size(12.5)).wrap());
                        });
                });
            },
        );
    }

    fn show_compact_instruction_panel(&mut self, ui: &mut egui::Ui, width: f32, height: f32) {
        self.record_rendered_affordance("compact-instruction-panel");
        ui.allocate_ui_with_layout(
            egui::vec2(width, height),
            egui::Layout::top_down(egui::Align::Min),
            |ui| {
                ui.set_width(width);
                self.show_compact_stage_summary(ui, height);
            },
        );
    }

    fn show_compact_help_chat_panel(&mut self, ui: &mut egui::Ui, width: f32, height: f32) {
        self.record_rendered_affordance("compact-help-chat-panel");
        let help_text = self.compact_help_chat_text();
        ui.allocate_ui_with_layout(
            egui::vec2(width, height),
            egui::Layout::top_down(egui::Align::Min),
            |ui| {
                ui::compact_panel_frame().show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 4.0;
                    ui.set_width(ui.available_width());
                    ui.set_min_height(compact_panel_inner_height(height));
                    ui.label(RichText::new("Помоги / чат").size(12.0).strong());
                    ui.label(
                        RichText::new(self.coach_state_label())
                            .size(10.5)
                            .color(egui::Color32::from_rgb(168, 180, 192)),
                    );
                    ui.separator();
                    egui::ScrollArea::vertical()
                        .id_salt("compact_help_chat_scroll")
                        .auto_shrink([false, false])
                        .max_height((height - 96.0).max(36.0))
                        .show(ui, |ui| {
                            ui.add(egui::Label::new(RichText::new(help_text).size(12.5)).wrap());
                        });
                });
            },
        );
    }

    fn compact_help_chat_text(&self) -> String {
        if let Some(text) = self
            .coach_live
            .as_deref()
            .filter(|text| !text.trim().is_empty() && !is_technical_coach_status(text))
        {
            return text.trim().to_string();
        }

        if let Some(text) = self
            .coach_bubbles
            .iter()
            .rev()
            .find(|text| !text.trim().is_empty() && !is_technical_coach_status(text))
        {
            return text.trim().to_string();
        }

        if let Some(message) = self.coach_chat_messages.iter().rev().find(|message| {
            message.role == CoachChatRole::Assistant && !message.text.trim().is_empty()
        }) {
            return message.text.trim().to_string();
        }

        if self.help_is_busy() {
            "Готовлю подсказку...".to_string()
        } else {
            "Нажмите «Помоги», чтобы получить следующую реплику или вопрос.".to_string()
        }
    }

    fn show_compact_resize_affordances(&mut self, ui: &mut egui::Ui, window_rect: egui::Rect) {
        self.record_rendered_affordance("compact-resize-handle");
        self.record_rendered_affordance("compact-resize-right");
        self.record_rendered_affordance("compact-resize-bottom");
        self.record_rendered_affordance("compact-resize-corner");

        let grip_color = egui::Color32::from_rgba_unmultiplied(220, 235, 245, 155);
        let edge_thickness = 18.0;
        let corner_size = 34.0;
        let border_rect = window_rect;
        let bottom_rect = egui::Rect::from_min_max(
            egui::pos2(
                border_rect.left() + 120.0,
                border_rect.bottom() - edge_thickness,
            ),
            egui::pos2(border_rect.right() - corner_size, border_rect.bottom()),
        );
        let right_rect = egui::Rect::from_min_max(
            egui::pos2(
                border_rect.right() - edge_thickness,
                border_rect.top() + 70.0,
            ),
            egui::pos2(border_rect.right(), border_rect.bottom() - corner_size),
        );
        let corner_rect = egui::Rect::from_min_max(
            egui::pos2(
                border_rect.right() - corner_size,
                border_rect.bottom() - corner_size,
            ),
            border_rect.right_bottom(),
        );

        let painter = ui.painter();

        painter.line_segment(
            [
                egui::pos2(bottom_rect.center().x - 34.0, bottom_rect.center().y + 2.0),
                egui::pos2(bottom_rect.center().x + 34.0, bottom_rect.center().y + 2.0),
            ],
            egui::Stroke::new(2.0, grip_color),
        );
        painter.line_segment(
            [
                egui::pos2(right_rect.center().x + 2.0, right_rect.center().y - 28.0),
                egui::pos2(right_rect.center().x + 2.0, right_rect.center().y + 28.0),
            ],
            egui::Stroke::new(2.0, grip_color),
        );
        for offset in [0.0, 7.0, 14.0] {
            painter.line_segment(
                [
                    egui::pos2(
                        corner_rect.right() - 24.0 + offset,
                        corner_rect.bottom() - 7.0,
                    ),
                    egui::pos2(
                        corner_rect.right() - 7.0,
                        corner_rect.bottom() - 24.0 + offset,
                    ),
                ],
                egui::Stroke::new(2.0, grip_color),
            );
        }
    }

    fn show_compact_stage_summary(&mut self, ui: &mut egui::Ui, height: f32) {
        let readiness_key = self
            .stage_agenda
            .as_ref()
            .and_then(|agenda| agenda.scorecard.as_ref())
            .map(|scorecard| scorecard.readiness.as_str())
            .unwrap_or("yellow");
        let signal = compact_readiness_color(readiness_key);
        let frame = ui::compact_signal_panel_frame(compact_readiness_stroke_color(readiness_key));
        let response = frame.show(ui, |ui| {
            ui.spacing_mut().item_spacing.y = 4.0;
            ui.set_width(ui.available_width());
            ui.set_min_height(compact_panel_inner_height(height));
            if let Some(agenda) = &self.stage_agenda {
                let label = agenda
                    .scorecard
                    .as_ref()
                    .map(|scorecard| {
                        format!(
                            "{} · {} · {} · {}/{}",
                            agenda.stage,
                            agenda.title,
                            scorecard.readiness_label,
                            scorecard.hit_count,
                            scorecard.total_count
                        )
                    })
                    .unwrap_or_else(|| format!("{} · {}", agenda.stage, agenda.title));
                ui.horizontal(|ui| {
                    ui.label(RichText::new("Инструкция").size(12.0).strong());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        compact_signal_badge(ui, compact_readiness_label(readiness_key), signal);
                    });
                });
                ui.label(RichText::new(label).size(10.5).strong().color(signal));
                ui.separator();
                let advice = agenda
                    .scorecard
                    .as_ref()
                    .map(|scorecard| speakable_stage_action(&scorecard.next_action))
                    .unwrap_or_else(|| speakable_stage_action(&agenda.step));
                egui::ScrollArea::vertical()
                    .id_salt("compact_instruction_scroll")
                    .auto_shrink([false, false])
                    .max_height((height - 104.0).max(44.0))
                    .show(ui, |ui| {
                        ui.add(
                            egui::Label::new(
                                RichText::new(advice)
                                    .size(15.5)
                                    .strong()
                                    .color(egui::Color32::from_rgb(241, 244, 247)),
                            )
                            .wrap(),
                        );
                    });
            } else {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("Инструкция").size(12.0).strong());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        compact_signal_badge(ui, "Желтый", signal);
                    });
                });
                ui.label(
                    RichText::new("Определяю стадию")
                        .size(10.5)
                        .strong()
                        .color(signal),
                );
                ui.separator();
                ui.add(
                    egui::Label::new(
                        RichText::new("Говорите, я слушаю и собираю контекст.")
                            .size(15.5)
                            .strong()
                            .color(egui::Color32::from_rgb(241, 244, 247)),
                    )
                    .wrap(),
                );
            }
        });
        let rect = response.response.rect;
        ui.painter().rect_filled(
            egui::Rect::from_min_max(
                egui::pos2(rect.left(), rect.top() + 2.0),
                egui::pos2(rect.left() + 3.0, rect.bottom() - 2.0),
            ),
            egui::CornerRadius::same(2),
            signal,
        );
    }

    fn show_top_bar(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("REC Sidecar").size(18.0).strong());
            ui.add_space(10.0);
            self.show_primary_controls(ctx, ui);
            if self.current_run.is_some() && self.control_button(ui, "Компактно").clicked()
            {
                self.expanded_workspace = false;
                self.applied_window_mode = None;
            }
            ui.add_space(8.0);
            self.show_asr_language_selector(ui);

            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.label(
                    RichText::new(self.app_state_label())
                        .size(13.0)
                        .color(self.app_state_color()),
                );
            });
        });
    }

    fn show_primary_controls(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        let primary_label = if self.connecting {
            "Отменить"
        } else if self.recording {
            "Остановить"
        } else if self.current_run.is_some() {
            "Продолжить"
        } else {
            "Начать REC"
        };

        self.record_rendered_control(primary_label);
        if ui
            .add(egui::Button::new(RichText::new(primary_label).strong()))
            .clicked()
        {
            if self.connecting || self.recording {
                self.pause_recording();
            } else if self.current_run.is_some() {
                self.continue_recording();
            } else {
                self.start_new_recording();
            }
        }

        if self
            .control_button_enabled(ui, !self.connecting, "Новый")
            .clicked()
        {
            self.start_new_recording();
        }

        if self
            .control_button_enabled(ui, self.current_run.is_some(), "Сохранить")
            .clicked()
        {
            if let Err(err) = self.save_current_run() {
                self.status = format!("Save failed: {}", err);
            }
        }

        if self
            .control_button_enabled(ui, self.current_run.is_some(), "Сохранить и закрыть")
            .clicked()
        {
            self.save_and_exit();
        }

        let help_busy = self.help_is_busy();
        let help_label = if help_busy {
            "готовлю..."
        } else {
            "Помоги"
        };
        if self
            .control_button_enabled(ui, self.current_run.is_some() && !help_busy, help_label)
            .clicked()
        {
            self.send_help_request();
        }

        if self.control_button(ui, "Выйти").clicked() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }

    fn show_live_center(&mut self, ui: &mut egui::Ui) {
        egui::ScrollArea::vertical()
            .id_salt("live_center_scroll")
            .auto_shrink([false, false])
            .show(ui, |ui| {
                self.show_stage_agenda_card(ui);
                ui.add_space(4.0);
                self.show_run_history_cards(ui);
                ui.add_space(8.0);
                ui.label(
                    RichText::new(&self.status)
                        .size(12.0)
                        .color(egui::Color32::from_rgb(168, 180, 192)),
                );
            });
    }

    fn show_idle_dashboard(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.add_space(10.0);
        ui::glass_panel_frame().show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Готов к звонку").size(22.0).strong());
                ui.label(
                    RichText::new(&self.status)
                        .size(13.0)
                        .color(egui::Color32::from_rgb(168, 180, 192)),
                );
            });
            ui.add_space(8.0);
            self.show_primary_controls(ctx, ui);
        });

        ui.add_space(4.0);
        self.show_run_history_cards(ui);
    }

    fn show_run_history_cards(&mut self, ui: &mut egui::Ui) {
        if ui.available_width() >= 700.0 {
            ui.columns(2, |columns| {
                ui::glass_panel_frame().show(&mut columns[0], |ui| {
                    ui.set_width(ui.available_width());
                    self.show_runs_menu(ui);
                });
                ui::glass_panel_frame().show(&mut columns[1], |ui| {
                    ui.set_width(ui.available_width());
                    self.show_history_menu(ui);
                });
            });
        } else {
            ui::glass_panel_frame().show(ui, |ui| {
                ui.set_width(ui.available_width());
                self.show_runs_menu(ui);
            });
            ui.add_space(4.0);
            ui::glass_panel_frame().show(ui, |ui| {
                ui.set_width(ui.available_width());
                self.show_history_menu(ui);
            });
        }
    }

    fn app_state_label(&self) -> &'static str {
        if self.connecting {
            "connecting"
        } else if self.recording {
            "listening"
        } else if self.current_run.is_some() {
            "paused"
        } else {
            "idle"
        }
    }

    fn app_state_color(&self) -> egui::Color32 {
        if self.connecting {
            egui::Color32::from_rgb(255, 190, 95)
        } else if self.recording {
            egui::Color32::from_rgb(92, 231, 158)
        } else if self.current_run.is_some() {
            egui::Color32::from_rgb(181, 197, 214)
        } else {
            egui::Color32::from_rgb(124, 137, 151)
        }
    }

    fn transcript_state_label(&self) -> &'static str {
        if self.connecting {
            "подключаюсь"
        } else if self.recording {
            "слушаю"
        } else if self.current_run.is_some() {
            "пауза"
        } else {
            "пусто"
        }
    }

    fn coach_state_label(&self) -> &str {
        self.coach_status.lines().next().unwrap_or("Coach idle")
    }

    fn show_asr_language_selector(&mut self, ui: &mut egui::Ui) {
        let before = self.asr_language_selection;

        ui.label("Язык");
        egui::ComboBox::from_id_salt("asr_language_selector")
            .selected_text(self.asr_language_selection.label())
            .show_ui(ui, |ui| {
                for selection in AsrLanguageSelection::ALL {
                    ui.selectable_value(
                        &mut self.asr_language_selection,
                        selection,
                        selection.label(),
                    );
                }
            });

        if self.asr_language_selection != before {
            self.status = format!("STT language: {}", self.asr_language_selection.label());

            if self.recording || self.connecting {
                self.spawn_worker();
            }
        }
    }

    fn show_runs_menu(&mut self, ui: &mut egui::Ui) {
        ui.heading("Текущий звонок");
        ui.add_space(8.0);

        let title = self
            .current_run
            .as_ref()
            .map(|run| run.title.as_str())
            .unwrap_or("Нет активного звонка");
        ui.label(title);

        let state = if self.connecting {
            "Подключаю транскрибацию..."
        } else if self.recording {
            "Транскрибация идет"
        } else if self.current_run.is_some() {
            "Пауза, можно продолжить или сохранить"
        } else {
            "Нажмите REC, чтобы начать"
        };
        ui.label(state);
        ui.add_space(12.0);

        if ui
            .add_enabled(self.current_run.is_some(), egui::Button::new("Сохранить"))
            .clicked()
        {
            if let Err(err) = self.save_current_run() {
                self.status = format!("Save failed: {}", err);
            }
        }

        if ui
            .add_enabled(!self.connecting, egui::Button::new("Новый REC"))
            .clicked()
        {
            self.start_new_recording();
        }
    }

    fn show_history_menu(&mut self, ui: &mut egui::Ui) {
        ui.heading("История");
        ui.add_space(8.0);

        ui.horizontal_wrapped(|ui| {
            if self.control_button(ui, "Посмотреть").clicked() {
                self.view_selected_history();
            }

            if self.control_button(ui, "Выгрузить").clicked() {
                self.export_selected_history();
            }

            if self.control_button(ui, "Обновить").clicked() {
                self.refresh_history();
                self.status = "History refreshed".to_string();
            }
        });

        ui.add_space(8.0);
        egui::ScrollArea::vertical().show(ui, |ui| {
            if self.history.is_empty() {
                ui.label("Сохраненных звонков пока нет");
                return;
            }

            for (index, run) in self.history.iter().enumerate() {
                let selected = self.selected_history == Some(index);
                let label = format!("{}\n{}", run.title, run.modified);

                if ui.selectable_label(selected, label).clicked() {
                    self.selected_history = Some(index);
                }
            }
        });
    }

    fn show_stage_agenda_card(&mut self, ui: &mut egui::Ui) {
        let readiness_key = self
            .stage_agenda
            .as_ref()
            .and_then(|agenda| agenda.scorecard.as_ref())
            .map(|scorecard| scorecard.readiness.as_str())
            .unwrap_or("pending");

        ui::tinted_glass_frame(
            readiness_panel_fill(readiness_key),
            readiness_stroke_color(readiness_key),
        )
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            if let Some(agenda) = &self.stage_agenda {
                let readiness = agenda.scorecard.as_ref().map(|scorecard| {
                    (
                        scorecard.readiness.as_str(),
                        scorecard.readiness_label.as_str(),
                    )
                });
                let readiness_key = readiness.map(|(key, _)| key).unwrap_or("pending");
                let readiness_accent = readiness_color(readiness_key);

                egui::Frame::new()
                    .fill(readiness_accent)
                    .corner_radius(egui::CornerRadius::same(12))
                    .inner_margin(egui::Margin::symmetric(10, 6))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.horizontal_wrapped(|ui| {
                            if let Some(scorecard) = &agenda.scorecard {
                                let counter = if scorecard.total_count > 0 {
                                    format!(
                                        "{} · {} · {}/{}",
                                        agenda.stage,
                                        scorecard.readiness_label,
                                        scorecard.hit_count,
                                        scorecard.total_count
                                    )
                                } else {
                                    format!("{} · {}", agenda.stage, scorecard.readiness_label)
                                };
                                ui.label(
                                    RichText::new(counter)
                                        .color(egui::Color32::WHITE)
                                        .size(13.0)
                                        .strong(),
                                );
                            } else if let Some((_, label)) = readiness {
                                ui.label(
                                    RichText::new(format!("{} · {}", agenda.stage, label))
                                        .color(egui::Color32::WHITE)
                                        .size(13.0)
                                        .strong(),
                                );
                            } else {
                                ui.label(
                                    RichText::new(&agenda.stage)
                                        .color(egui::Color32::WHITE)
                                        .size(13.0)
                                        .strong(),
                                );
                            }

                            ui.label(
                                RichText::new(&agenda.title)
                                    .color(egui::Color32::WHITE)
                                    .size(12.0),
                            );
                            ui.with_layout(
                                egui::Layout::right_to_left(egui::Align::Center),
                                |ui| {
                                    ui.label(
                                        RichText::new(&agenda.model)
                                            .color(egui::Color32::WHITE)
                                            .italics()
                                            .size(11.0),
                                    );
                                },
                            );
                        });
                    });

                ui.add_space(10.0);
                if let Some(scorecard) = &agenda.scorecard {
                    ui.label(
                        RichText::new("СОВЕТ")
                            .color(readiness_accent)
                            .strong()
                            .size(12.0),
                    );
                    ui.add(
                        egui::Label::new(
                            RichText::new(speakable_stage_action(&scorecard.next_action))
                                .color(readiness_text_color(&scorecard.readiness))
                                .size(21.0)
                                .strong(),
                        )
                        .wrap(),
                    );
                    ui.add_space(8.0);
                    Self::show_score_signal_badges(ui, &scorecard.signals);
                    ui.add_space(8.0);
                    ui.label(
                        RichText::new(&scorecard.summary)
                            .color(readiness_text_color(&scorecard.readiness))
                            .size(13.0)
                            .italics(),
                    );
                    if let Some(check) = scorecard
                        .checks
                        .iter()
                        .find(|check| check.result == "miss" && check.level == "core")
                        .or_else(|| scorecard.checks.iter().find(|check| check.result == "miss"))
                        .or_else(|| {
                            scorecard
                                .checks
                                .iter()
                                .find(|check| check.result == "pending")
                        })
                    {
                        ui.add_space(4.0);
                        ui.label(
                            RichText::new(format!("Фокус: {}", check.reason))
                                .color(readiness_text_color(&scorecard.readiness))
                                .strong()
                                .size(13.0),
                        );
                    }
                } else {
                    ui.label(
                        RichText::new("Сказать сейчас")
                            .color(readiness_accent)
                            .strong()
                            .size(12.0),
                    );
                    ui.add(egui::Label::new(RichText::new(&agenda.emotion).size(18.0)).wrap());
                    ui.add_space(8.0);
                    ui.label(
                        RichText::new("Следующий ход")
                            .color(readiness_accent)
                            .strong()
                            .size(12.0),
                    );
                    ui.add(egui::Label::new(RichText::new(&agenda.step).size(18.0)).wrap());
                }

                ui.add_space(10.0);
                ui.separator();
                ui.add_space(6.0);
                ui.label(
                    RichText::new(format!("Цель стадии: {}", agenda.agenda))
                        .color(egui::Color32::from_rgb(178, 190, 203))
                        .size(12.5),
                );
            } else {
                egui::Frame::new()
                    .fill(readiness_color("pending"))
                    .corner_radius(egui::CornerRadius::same(12))
                    .inner_margin(egui::Margin::symmetric(10, 6))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.label(
                            RichText::new("Определяю стадию")
                                .color(egui::Color32::WHITE)
                                .size(13.0)
                                .strong(),
                        );
                    });
                ui.add_space(10.0);
                ui.label(
                    RichText::new("СОВЕТ")
                        .color(readiness_color("pending"))
                        .strong()
                        .size(12.0),
                );
                ui.add(
                    egui::Label::new(
                        RichText::new("Говорите, я слушаю и собираю контекст.")
                            .color(readiness_text_color("pending"))
                            .size(21.0)
                            .strong(),
                    )
                    .wrap(),
                );
                ui.add_space(8.0);
                ui.label(
                    RichText::new(&self.stage_status)
                        .color(readiness_text_color("pending"))
                        .size(12.5),
                );
            }
        });
    }

    fn show_score_signal_badges(ui: &mut egui::Ui, signals: &[coach::CoachStageScoreSignal]) {
        egui::Grid::new("stage_signal_badges")
            .num_columns(3)
            .spacing([8.0, 8.0])
            .show(ui, |ui| {
                for (index, signal) in signals.iter().enumerate() {
                    egui::Frame::new()
                        .fill(signal_badge_fill(&signal.state))
                        .stroke(egui::Stroke::new(1.0, signal_color(&signal.state)))
                        .corner_radius(egui::CornerRadius::same(9))
                        .inner_margin(egui::Margin::symmetric(8, 4))
                        .show(ui, |ui| {
                            ui.set_min_width(88.0);
                            ui.label(
                                RichText::new(&signal.label)
                                    .color(signal_color(&signal.state))
                                    .strong()
                                    .size(12.0),
                            );
                        });

                    if (index + 1) % 3 == 0 {
                        ui.end_row();
                    }
                }
            });
    }

    fn show_transcript_column(&mut self, ui: &mut egui::Ui) {
        let scroll_height = (ui.clip_rect().bottom() - ui.cursor().top()).max(160.0);
        egui::ScrollArea::vertical()
            .id_salt("transcript_scroll")
            .auto_shrink([false, false])
            .max_height(scroll_height)
            .show(ui, |ui| {
                if self.bubbles.is_empty() && self.live_partial.is_none() {
                    ui.label("Transcript is empty");
                } else {
                    for text in &self.bubbles {
                        ui::draw_bubble(ui, text);
                    }

                    if let Some(partial) = &self.live_partial {
                        ui::draw_live_bubble(ui, partial);
                    }
                }
            });
    }

    fn show_coach_column(&mut self, ui: &mut egui::Ui) {
        ui.label(
            RichText::new(self.coach_state_label())
                .size(13.0)
                .color(egui::Color32::from_rgb(168, 180, 192)),
        );
        ui.separator();

        let composer_height = 106.0;
        let scroll_height =
            (ui.clip_rect().bottom() - ui.cursor().top() - composer_height).max(160.0);
        egui::ScrollArea::vertical()
            .id_salt("coach_chat_scroll")
            .stick_to_bottom(true)
            .auto_shrink([false, false])
            .max_height(scroll_height)
            .show(ui, |ui| {
                let has_live_suggestions = self
                    .coach_bubbles
                    .iter()
                    .any(|text| !is_technical_coach_status(text))
                    || self
                        .coach_live
                        .as_ref()
                        .is_some_and(|text| !is_technical_coach_status(text));

                if has_live_suggestions {
                    ui.label(RichText::new("Live-подсказки").strong());
                    ui.add_space(4.0);

                    for text in &self.coach_bubbles {
                        if is_technical_coach_status(text) {
                            continue;
                        }
                        ui::draw_coach_bubble(ui, text, false);
                    }

                    if let Some(partial) = &self.coach_live {
                        if !is_technical_coach_status(partial) {
                            ui::draw_coach_bubble(ui, partial, true);
                        }
                    }

                    ui.separator();
                    ui.add_space(4.0);
                }

                ui.label(RichText::new("Чат").strong());
                ui.add_space(4.0);

                if self.coach_chat_messages.is_empty() {
                    ui.label("Чат пуст");
                } else {
                    for message in &self.coach_chat_messages {
                        ui::draw_chat_message(ui, message);
                    }
                }
            });

        ui.separator();
        ui.horizontal(|ui| {
            let button_width = 104.0;
            let input_width =
                (ui.available_width() - button_width - ui.spacing().item_spacing.x).max(220.0);
            let input = egui::TextEdit::multiline(&mut self.coach_chat_input)
                .desired_width(input_width)
                .desired_rows(3);
            let response = ui.add(input);
            let send_shortcut = response.has_focus()
                && ui.input(|input| {
                    input.key_pressed(egui::Key::Enter)
                        && (input.modifiers.command || input.modifiers.ctrl)
                });

            ui.vertical(|ui| {
                self.record_rendered_control("Отправить");
                if ui
                    .add_sized([button_width, 28.0], egui::Button::new("Отправить"))
                    .clicked()
                    || send_shortcut
                {
                    self.send_typed_chat_question();
                }

                let help_busy = self.help_is_busy();
                let help_label = if help_busy {
                    "готовлю..."
                } else {
                    "Помоги"
                };
                self.record_rendered_control(help_label);
                if ui
                    .add_enabled_ui(!help_busy, |ui| {
                        ui.add_sized([button_width, 28.0], egui::Button::new(help_label))
                    })
                    .inner
                    .clicked()
                {
                    self.send_help_request();
                }
            });
        });
    }

    fn show_history_viewer(&mut self, ctx: &egui::Context) {
        ctx.show_viewport_immediate(
            egui::ViewportId::from_hash_of("history_viewer"),
            egui::ViewportBuilder::default()
                .with_title(self.viewer_title.clone())
                .with_inner_size([640.0, 720.0])
                .with_min_inner_size([460.0, 420.0]),
            |ctx, class| {
                if class == egui::ViewportClass::Embedded {
                    return;
                }

                egui::CentralPanel::default().show(ctx, |ui| {
                    ui.heading(&self.viewer_title);
                    ui.separator();
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        ui.add(
                            egui::TextEdit::multiline(&mut self.viewer_text)
                                .desired_width(f32::INFINITY)
                                .interactive(false),
                        );
                    });
                });

                if ctx.input(|input| input.viewport().close_requested()) {
                    self.viewer_open = false;
                }
            },
        );
    }
}

fn readiness_color(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgb(31, 202, 111),
        "yellow" => egui::Color32::from_rgb(245, 184, 50),
        "red" => egui::Color32::from_rgb(238, 72, 86),
        _ => egui::Color32::from_rgb(237, 112, 55),
    }
}

fn compact_readiness_color(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgb(67, 209, 125),
        "red" => egui::Color32::from_rgb(238, 106, 111),
        _ => egui::Color32::from_rgb(229, 184, 77),
    }
}

fn compact_readiness_stroke_color(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgba_unmultiplied(67, 209, 125, 118),
        "red" => egui::Color32::from_rgba_unmultiplied(238, 106, 111, 122),
        _ => egui::Color32::from_rgba_unmultiplied(229, 184, 77, 118),
    }
}

fn compact_readiness_label(readiness: &str) -> &'static str {
    match readiness {
        "green" => "Зеленый",
        "red" => "Красный",
        _ => "Желтый",
    }
}

fn compact_signal_badge(ui: &mut egui::Ui, label: &str, color: egui::Color32) {
    let fill = egui::Color32::from_rgba_unmultiplied(color.r(), color.g(), color.b(), 32);
    egui::Frame::new()
        .fill(fill)
        .stroke(egui::Stroke::new(
            1.0,
            egui::Color32::from_rgba_unmultiplied(color.r(), color.g(), color.b(), 112),
        ))
        .corner_radius(egui::CornerRadius::same(10))
        .inner_margin(egui::Margin::symmetric(7, 3))
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                let center = ui.cursor().min + egui::vec2(3.0, 6.0);
                ui.painter().circle_filled(center, 3.0, color);
                ui.add_space(9.0);
                ui.label(RichText::new(label).size(10.0).strong().color(color));
            });
        });
}

fn compact_panel_inner_height(outer_height: f32) -> f32 {
    (outer_height - 32.0).max(56.0)
}

fn compact_section_separator(ui: &mut egui::Ui, height: f32) {
    let (rect, _) = ui.allocate_exact_size(
        egui::vec2(ui.available_width(), height),
        egui::Sense::hover(),
    );
    ui.painter().line_segment(
        [
            egui::pos2(rect.left(), rect.center().y),
            egui::pos2(rect.right(), rect.center().y),
        ],
        egui::Stroke::new(
            1.0,
            egui::Color32::from_rgba_unmultiplied(255, 255, 255, 28),
        ),
    );
}

fn compact_vertical_separator(ui: &mut egui::Ui, width: f32, height: f32) {
    let (rect, _) = ui.allocate_exact_size(egui::vec2(width, height), egui::Sense::hover());
    ui.painter().line_segment(
        [
            egui::pos2(rect.center().x, rect.top()),
            egui::pos2(rect.center().x, rect.bottom()),
        ],
        egui::Stroke::new(
            1.0,
            egui::Color32::from_rgba_unmultiplied(255, 255, 255, 54),
        ),
    );
}

fn readiness_panel_fill(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgba_unmultiplied(30, 190, 102, 54),
        "yellow" => egui::Color32::from_rgba_unmultiplied(245, 184, 50, 58),
        "red" => egui::Color32::from_rgba_unmultiplied(238, 72, 86, 64),
        _ => egui::Color32::from_rgba_unmultiplied(237, 112, 55, 54),
    }
}

fn readiness_stroke_color(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgba_unmultiplied(94, 245, 163, 150),
        "yellow" => egui::Color32::from_rgba_unmultiplied(255, 217, 104, 155),
        "red" => egui::Color32::from_rgba_unmultiplied(255, 117, 128, 165),
        _ => egui::Color32::from_rgba_unmultiplied(255, 151, 91, 150),
    }
}

fn readiness_text_color(readiness: &str) -> egui::Color32 {
    match readiness {
        "green" => egui::Color32::from_rgb(202, 255, 224),
        "yellow" => egui::Color32::from_rgb(255, 237, 173),
        "red" => egui::Color32::from_rgb(255, 213, 217),
        _ => egui::Color32::from_rgb(255, 219, 195),
    }
}

fn signal_color(state: &str) -> egui::Color32 {
    match state {
        "green" => egui::Color32::from_rgb(101, 244, 164),
        "yellow" => egui::Color32::from_rgb(255, 217, 104),
        "red" => egui::Color32::from_rgb(255, 132, 142),
        _ => egui::Color32::from_rgb(184, 195, 207),
    }
}

fn signal_badge_fill(state: &str) -> egui::Color32 {
    match state {
        "green" => egui::Color32::from_rgba_unmultiplied(30, 190, 102, 42),
        "yellow" => egui::Color32::from_rgba_unmultiplied(245, 184, 50, 42),
        "red" => egui::Color32::from_rgba_unmultiplied(238, 72, 86, 46),
        _ => egui::Color32::from_rgba_unmultiplied(255, 255, 255, 28),
    }
}

fn is_technical_coach_status(text: &str) -> bool {
    let text = text.trim_start();
    text.starts_with("Coach ready\nservice:") || text.starts_with("Coach connecting")
}

impl eframe::App for RecApp {
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        [0.0, 0.0, 0.0, 0.0]
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.drain_asr();
        self.drain_coach();
        self.maybe_dispatch_pending_help();
        if self.auto_start_pending {
            self.auto_start_pending = false;
            if self.current_run.is_none() && !self.recording && !self.connecting {
                self.start_new_recording();
            }
        }

        let help_shortcut = ctx.input(|input| {
            input.key_pressed(egui::Key::H)
                && input.modifiers.shift
                && (input.modifiers.command || input.modifiers.ctrl)
        });
        if help_shortcut {
            if self.help_is_busy() {
                self.status = "Coach is already preparing help".to_string();
            } else {
                self.send_help_request();
            }
        }

        if coach_auto_suggestions_enabled() {
            self.maybe_send_coach_snapshot();
        }
        self.maybe_send_stage_request();
        self.show_main_window(ctx);

        if self.viewer_open {
            self.show_history_viewer(ctx);
        }

        if self.recording
            || self.connecting
            || self.coach_live.is_some()
            || self.chat_is_streaming()
            || self.pending_help.is_some()
        {
            ctx.request_repaint_after(Duration::from_millis(100));
        }
    }
}

fn help_context_delay() -> Duration {
    Duration::from_millis(env_u64(
        "COACH_HELP_CONTEXT_DELAY_MS",
        DEFAULT_HELP_CONTEXT_DELAY_MS,
    ))
}

fn coach_auto_suggestions_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| env_bool("COACH_AUTO_SUGGESTIONS", true))
}

fn stage_detect_interval() -> Duration {
    Duration::from_millis(env_u64(
        "COACH_STAGE_DETECT_INTERVAL_MS",
        DEFAULT_STAGE_DETECT_INTERVAL_MS,
    ))
}

fn stage_audio_live_enabled() -> bool {
    env_flag("COACH_STAGE_AUDIO_LIVE")
        || env_flag("COACH_STAGE_LIVE_AUDIO")
        || env_flag("COACH_STAGE_AUDIO_LIVE_ENABLED")
}

fn stage_rest_fallback_enabled() -> bool {
    env_flag("COACH_STAGE_REST_FALLBACK")
}

fn stage_rank(stage: &str) -> Option<usize> {
    match stage.trim().to_ascii_lowercase().as_str() {
        "s2.1" => Some(0),
        "s2.2" => Some(1),
        "s2.3" => Some(2),
        "s2.4" => Some(3),
        "s2.5" => Some(4),
        "s3.1" => Some(5),
        "s3.2" => Some(6),
        "s3.3" => Some(7),
        "s3.4a" => Some(8),
        "s3.4b" => Some(9),
        "s3.5" => Some(10),
        _ => None,
    }
}

fn stage_is_backward(current_stage: &str, incoming_stage: &str) -> bool {
    matches!(
        (stage_rank(current_stage), stage_rank(incoming_stage)),
        (Some(current), Some(incoming)) if incoming < current
    )
}

fn speakable_stage_action(text: &str) -> &str {
    let trimmed = text.trim();
    for prefix in [
        "Уточнить:",
        "Переход:",
        "Сказать:",
        "Скажите:",
        "Скажи:",
        "Спросить:",
    ] {
        if let Some(rest) = trimmed.strip_prefix(prefix) {
            return rest.trim();
        }
    }
    trimmed
}

fn stage_ui_state_path() -> PathBuf {
    env_var("REC_STAGE_UI_STATE_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_STAGE_UI_STATE_PATH))
}

fn write_json_atomic(path: &Path, value: &Value) -> io::Result<()> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }

    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("stage-ui.json");
    let tmp_path = path.with_file_name(format!(".{}.tmp", file_name));
    let bytes = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    fs::write(&tmp_path, bytes)?;
    fs::rename(tmp_path, path)
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

fn env_flag(name: &str) -> bool {
    env_bool(name, false)
}

fn env_usize(name: &str, default: usize) -> usize {
    env_var(name)
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::*;
    use eframe::App as _;

    fn temp_paths() -> (tempfile::TempDir, AppPaths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(dir.path().join("runs"), dir.path().join("exports"));
        (dir, paths)
    }

    fn run_headless_frame(app: &mut RecApp) -> egui::FullOutput {
        app.rendered_controls.clear();
        app.rendered_affordances.clear();
        let ctx = egui::Context::default();
        let mut frame = eframe::Frame::_new_kittest();
        ctx.run(egui::RawInput::default(), |ctx| {
            app.update(ctx, &mut frame);
        })
    }

    fn active_test_app(paths: AppPaths) -> RecApp {
        let mut app = RecApp::new_with_paths(paths);
        app.current_run = Some(RunSession {
            id: "run".to_string(),
            title: "Run".to_string(),
            path: None,
        });
        app.recording = true;
        app.coach_status = "Coach ready".to_string();
        app.stage_agenda = Some(coach::CoachStageAgenda {
            stage: "S2.3".to_string(),
            title: "Target & Gap".to_string(),
            agenda: "выяснить желаемый результат".to_string(),
            emotion: "Очень крутая цель.".to_string(),
            step: "Почему пока не получается?".to_string(),
            provider: "cerebras".to_string(),
            model: "gpt-oss-120b".to_string(),
            scorecard: None,
        });
        app
    }

    fn assert_controls_include(app: &RecApp, expected: &[&str]) {
        for label in expected {
            assert!(
                app.rendered_controls.iter().any(|actual| actual == label),
                "missing UI control `{}`; rendered controls: {:?}",
                label,
                app.rendered_controls
            );
        }
    }

    fn assert_affordances_include(app: &RecApp, expected: &[&str]) {
        for label in expected {
            assert!(
                app.rendered_affordances
                    .iter()
                    .any(|actual| actual == label),
                "missing UI affordance `{}`; rendered affordances: {:?}",
                label,
                app.rendered_affordances
            );
        }
    }

    fn root_viewport_commands(output: &egui::FullOutput) -> &[egui::ViewportCommand] {
        output
            .viewport_output
            .get(&egui::ViewportId::ROOT)
            .map(|viewport| viewport.commands.as_slice())
            .unwrap_or(&[])
    }

    fn command_inner_size(commands: &[egui::ViewportCommand]) -> Option<egui::Vec2> {
        commands.iter().find_map(|command| match command {
            egui::ViewportCommand::InnerSize(size) => Some(*size),
            _ => None,
        })
    }

    #[test]
    fn default_app_renders_dashboard_headlessly() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);

        let _ = run_headless_frame(&mut app);

        assert_eq!(app.status, "Ready");
        assert!(!app.recording);
        assert!(app.history.is_empty());
    }

    #[test]
    fn compact_overlay_renders_critical_controls_and_window_mode() {
        let (_dir, paths) = temp_paths();
        let mut app = active_test_app(paths);
        app.expanded_workspace = false;

        let output = run_headless_frame(&mut app);

        assert_controls_include(
            &app,
            &[
                "Стоп",
                "Помоги",
                "Развернуть",
                "Сохранить",
                "Сохр. и закрыть",
                "Выйти",
            ],
        );
        assert_affordances_include(
            &app,
            &[
                "compact-drag-header",
                "compact-resize-handle",
                "compact-resize-right",
                "compact-resize-bottom",
                "compact-resize-corner",
                "compact-layout-horizontal",
                "compact-transcript-panel",
                "compact-instruction-panel",
                "compact-help-chat-panel",
            ],
        );
        let commands = root_viewport_commands(&output);
        assert!(
            commands
                .iter()
                .any(|command| matches!(command, egui::ViewportCommand::Transparent(true))),
            "compact overlay must keep transparent viewport; commands: {:?}",
            commands
        );
        assert!(
            commands
                .iter()
                .any(|command| matches!(command, egui::ViewportCommand::Decorations(false))),
            "compact overlay must be borderless; commands: {:?}",
            commands
        );
        assert!(
            commands
                .iter()
                .any(|command| matches!(command, egui::ViewportCommand::Resizable(true))),
            "compact overlay should keep native resize; commands: {:?}",
            commands
        );
        assert!(
            commands.iter().any(|command| matches!(
                command,
                egui::ViewportCommand::WindowLevel(egui::WindowLevel::AlwaysOnTop)
            )),
            "compact overlay must stay above the call app; commands: {:?}",
            commands
        );
        assert!(
            commands.iter().any(|command| matches!(
                command,
                egui::ViewportCommand::InnerSize(size)
                    if size.x <= 1_000.0 && size.y >= COMPACT_HORIZONTAL_MIN_HEIGHT
            )),
            "compact overlay must stay compact but tall enough to keep every panel visible; commands: {:?}",
            commands
        );
    }

    #[test]
    fn compact_vertical_layout_keeps_all_workflow_panels_visible() {
        let (_dir, paths) = temp_paths();
        let mut app = active_test_app(paths);
        app.expanded_workspace = false;
        app.compact_size = egui::vec2(520.0, COMPACT_VERTICAL_MIN_HEIGHT);

        let output = run_headless_frame(&mut app);

        assert_affordances_include(
            &app,
            &[
                "compact-layout-vertical",
                "compact-transcript-panel",
                "compact-instruction-panel",
                "compact-help-chat-panel",
            ],
        );
        let commands = root_viewport_commands(&output);
        assert!(
            matches!(
                command_inner_size(commands),
                Some(size)
                    if size.x == 520.0 && size.y >= COMPACT_VERTICAL_MIN_HEIGHT
            ),
            "vertical compact layout must reserve enough height for every panel; commands: {:?}",
            commands
        );
    }

    #[test]
    fn compact_manual_resize_clamps_and_emits_inner_size() {
        let (_dir, paths) = temp_paths();
        let mut app = active_test_app(paths);
        let ctx = egui::Context::default();

        let output = ctx.run(egui::RawInput::default(), |ctx| {
            app.resize_compact_overlay(ctx, egui::vec2(920.0, 360.0));
        });
        let commands = root_viewport_commands(&output);
        assert_eq!(app.compact_size.x, 920.0);
        assert_eq!(app.compact_size.y, 360.0);
        assert!(
            matches!(command_inner_size(commands), Some(size) if size.x == 920.0 && size.y == 360.0),
            "manual resize must send the new compact inner size; commands: {:?}",
            commands
        );

        let output = ctx.run(egui::RawInput::default(), |ctx| {
            app.resize_compact_overlay(ctx, egui::vec2(320.0, 120.0));
        });
        let commands = root_viewport_commands(&output);
        assert_eq!(app.compact_size.x, COMPACT_MIN_WIDTH);
        assert_eq!(app.compact_size.y, COMPACT_VERTICAL_MIN_HEIGHT);
        assert!(
            matches!(
                command_inner_size(commands),
                Some(size)
                    if size.x == COMPACT_MIN_WIDTH && size.y == COMPACT_VERTICAL_MIN_HEIGHT
            ),
            "manual resize must clamp so workflow panels cannot disappear; commands: {:?}",
            commands
        );
    }

    #[test]
    fn compact_height_survives_expand_and_return() {
        let (_dir, paths) = temp_paths();
        let mut app = active_test_app(paths);
        app.expanded_workspace = false;
        app.compact_size.y = 336.0;

        let output = run_headless_frame(&mut app);
        let commands = root_viewport_commands(&output);
        assert!(
            matches!(command_inner_size(commands), Some(size) if size.y == 336.0),
            "compact should enter with stored manual height; commands: {:?}",
            commands
        );

        app.expanded_workspace = true;
        app.applied_window_mode = None;
        let _ = run_headless_frame(&mut app);

        app.expanded_workspace = false;
        app.applied_window_mode = None;
        let output = run_headless_frame(&mut app);
        let commands = root_viewport_commands(&output);
        assert!(
            matches!(command_inner_size(commands), Some(size) if size.y == 336.0),
            "compact should preserve manual height after expanded mode; commands: {:?}",
            commands
        );
    }

    #[test]
    fn expanded_workspace_renders_full_control_surface() {
        let (_dir, paths) = temp_paths();
        let mut app = active_test_app(paths);
        app.expanded_workspace = true;

        let output = run_headless_frame(&mut app);

        assert_controls_include(
            &app,
            &[
                "Остановить",
                "Новый",
                "Сохранить",
                "Сохранить и закрыть",
                "Помоги",
                "Выйти",
                "Компактно",
                "Посмотреть",
                "Выгрузить",
                "Обновить",
                "Отправить",
            ],
        );
        let commands = root_viewport_commands(&output);
        assert!(
            commands.iter().any(|command| matches!(
                command,
                egui::ViewportCommand::WindowLevel(egui::WindowLevel::Normal)
            )),
            "expanded workspace should be a normal window; commands: {:?}",
            commands
        );
    }

    #[test]
    fn asr_events_update_recording_state_without_ui() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);

        app.handle_asr_event(asr::AsrEvent::Connecting("connecting".to_string()));
        assert!(app.connecting);
        assert!(!app.recording);

        app.handle_asr_event(asr::AsrEvent::Ready("ready".to_string()));
        assert!(!app.connecting);
        assert!(app.recording);

        app.handle_asr_event(asr::AsrEvent::PartialTranscript("partial".to_string()));
        assert_eq!(app.live_partial.as_deref(), Some("partial"));

        app.handle_asr_event(asr::AsrEvent::Transcript("final".to_string()));
        assert_eq!(app.bubbles, vec!["final"]);
        assert!(app.live_partial.is_none());
    }

    #[test]
    fn asr_recovery_clears_live_partial_without_transcript_bubble() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);

        app.handle_asr_event(asr::AsrEvent::Ready("ready".to_string()));
        app.handle_asr_event(asr::AsrEvent::PartialTranscript("partial".to_string()));
        app.handle_asr_event(asr::AsrEvent::Recovering(
            "восстанавливаю STT... попытка 1/3 через 0.8 сек".to_string(),
        ));

        assert!(app.connecting);
        assert!(!app.recording);
        assert!(app.live_partial.is_none());
        assert!(app.bubbles.is_empty());
        assert_eq!(
            app.status,
            "восстанавливаю STT... попытка 1/3 через 0.8 сек"
        );

        app.handle_asr_event(asr::AsrEvent::Ready("ready again".to_string()));
        app.handle_asr_event(asr::AsrEvent::Transcript("final".to_string()));

        assert!(!app.connecting);
        assert!(app.recording);
        assert_eq!(app.bubbles, vec!["final"]);
    }

    #[test]
    fn coach_chat_events_update_matching_message() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);
        app.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::Assistant,
            text: String::new(),
            request_id: Some(7),
            streaming: false,
            help_stage_label: None,
            model_label: None,
        });

        app.handle_coach_event(coach::CoachEvent::ChatStarted(7));
        app.handle_coach_event(coach::CoachEvent::ChatDelta(7, "hello".to_string()));
        app.handle_coach_event(coach::CoachEvent::ChatModel(
            7,
            "gemini-3.5-flash".to_string(),
        ));
        app.handle_coach_event(coach::CoachEvent::ChatModel(
            7,
            "gemini-3.5-flash".to_string(),
        ));
        app.handle_coach_event(coach::CoachEvent::ChatModel(7, "gpt-oss-120b".to_string()));
        app.handle_coach_event(coach::CoachEvent::ChatFinished(7));

        let message = app.chat_message_mut(7).unwrap();
        assert_eq!(message.text, "hello");
        assert_eq!(
            message.model_label.as_deref(),
            Some("gemini-3.5-flash + gpt-oss-120b")
        );
        assert!(!message.streaming);
    }

    #[test]
    fn help_stage_events_update_status_and_message() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);
        app.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::Assistant,
            text: String::new(),
            request_id: Some(9),
            streaming: true,
            help_stage_label: None,
            model_label: None,
        });

        app.handle_coach_event(coach::CoachEvent::HelpStage(
            9,
            coach::CoachHelpStage::PreparingConstructive,
        ));

        assert_eq!(app.status, "Помоги: дополняю следующий ход");
        assert_eq!(app.coach_status, "Помоги: дополняю следующий ход");
        assert_eq!(
            app.chat_message_mut(9).unwrap().help_stage_label,
            Some("дополняю следующий ход")
        );

        app.handle_coach_event(coach::CoachEvent::ChatFinished(9));
        assert!(app.chat_message_mut(9).unwrap().help_stage_label.is_none());
    }

    #[test]
    fn stage_agenda_event_updates_overlay_state() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);

        app.handle_coach_event(coach::CoachEvent::StageAgenda(
            coach::CoachStageAgenda {
                stage: "S2.3".to_string(),
                title: "Target & Gap".to_string(),
                agenda: "выяснить желаемый результат".to_string(),
                emotion: "Очень крутая цель.".to_string(),
                step: "Почему пока не получается?".to_string(),
                provider: "cerebras".to_string(),
                model: "gpt-oss-120b".to_string(),
                scorecard: None,
            }
            .into(),
        ));

        let agenda = app.stage_agenda.as_ref().unwrap();
        assert_eq!(agenda.stage, "S2.3");
        assert_eq!(agenda.model, "gpt-oss-120b");
        assert_eq!(app.stage_status, "S2.3 · gpt-oss-120b");
    }

    #[test]
    fn stage_agenda_does_not_move_backward() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);
        app.apply_stage_agenda(coach::CoachStageAgenda {
            stage: "S2.3".to_string(),
            title: "Target & Gap".to_string(),
            agenda: "выяснить желаемый результат".to_string(),
            emotion: "Очень крутая цель.".to_string(),
            step: "Почему пока не получается?".to_string(),
            provider: "vertex-live-audio".to_string(),
            model: "gemini-live".to_string(),
            scorecard: None,
        });

        app.apply_stage_agenda(coach::CoachStageAgenda {
            stage: "S2.2".to_string(),
            title: "Current Reality".to_string(),
            agenda: "узнать текущую ситуацию".to_string(),
            emotion: "Понимаю.".to_string(),
            step: "Подскажи, что сейчас с финансами?".to_string(),
            provider: "vertex-live-audio".to_string(),
            model: "gemini-live".to_string(),
            scorecard: None,
        });

        let agenda = app.stage_agenda.as_ref().unwrap();
        assert_eq!(agenda.stage, "S2.3");
        assert!(app.stage_status.contains("откат S2.2 игнорирован"));
    }

    #[test]
    fn stage_action_display_strips_instruction_prefix() {
        assert_eq!(
            speakable_stage_action("Уточнить: Что нужно организовать до 7 июля?"),
            "Что нужно организовать до 7 июля?"
        );
        assert_eq!(
            speakable_stage_action("Переход: Давайте я расскажу про формат."),
            "Давайте я расскажу про формат."
        );
    }

    #[test]
    fn save_refreshes_history_with_injected_paths() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);
        app.current_run = Some(RunSession {
            id: "smoke".to_string(),
            title: "Smoke".to_string(),
            path: None,
        });
        app.bubbles.push("Спикер 1: hello".to_string());

        let path = app.save_current_run().unwrap();

        assert!(path.ends_with("smoke.txt"));
        assert_eq!(app.history.len(), 1);
        assert_eq!(app.selected_history, Some(0));
    }

    #[test]
    fn streaming_chat_and_pending_help_render_headlessly() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);
        app.current_run = Some(RunSession {
            id: "run".to_string(),
            title: "Run".to_string(),
            path: None,
        });
        app.coach_chat_messages.push(CoachChatMessage {
            role: CoachChatRole::Assistant,
            text: String::new(),
            request_id: Some(1),
            streaming: true,
            help_stage_label: None,
            model_label: None,
        });
        app.pending_help = Some(PendingHelpRequest {
            id: 1,
            run_id: "run".to_string(),
            due_at: Instant::now() + Duration::from_secs(60),
            created_at: Instant::now(),
        });

        let _ = run_headless_frame(&mut app);

        assert!(app.help_is_busy());
    }
}
