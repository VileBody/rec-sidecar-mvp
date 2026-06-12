use crate::{
    asr, coach,
    context::{self, CoachChatMessage, CoachChatRole, ContextInput, HelpContextSettings},
    session::{self, AppPaths, RunSession, SavedRun},
    ui,
};
use chrono::Local;
use eframe::egui::{self, RichText};
use std::{
    env, io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc,
    },
    time::{Duration, Instant},
};

const COACH_AUTO_SUGGESTIONS: bool = false;
const DEFAULT_HELP_CONTEXT_DELAY_MS: u64 = 300;
const DEFAULT_STAGE_DETECT_INTERVAL_MS: u64 = 5_000;
const DEFAULT_CONTEXT_MAX_TRANSCRIPT_CHARS: usize = 16_000;
const DEFAULT_CONTEXT_MAX_CHAT_CHARS: usize = 4_000;
const DEFAULT_CONTEXT_MAX_LIVE_PARTIAL_CHARS: usize = 1_200;
const DEFAULT_CONTEXT_MAX_COACH_MESSAGES: usize = 6;

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
                let agenda = *agenda;
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
            force: self.force_coach_snapshot,
        };

        if tx.send(coach::CoachInput::Snapshot(snapshot)).is_ok() {
            self.force_coach_snapshot = false;
            self.last_coach_snapshot_sent = Instant::now();
        }
    }

    fn maybe_send_stage_request(&mut self) {
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
                self.show_top_bar(ui);
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
                    self.show_idle_dashboard(ui);
                });
        }
    }

    fn apply_window_mode(&mut self, ctx: &egui::Context, mode: WindowMode) {
        if self.applied_window_mode == Some(mode) {
            return;
        }

        let monitor = ctx.input(|input| {
            input
                .viewport()
                .monitor_size
                .unwrap_or_else(|| egui::vec2(1440.0, 900.0))
        });
        let (size, min_size, position, window_level) = match mode {
            WindowMode::Compact => {
                let size = egui::vec2(980.0_f32.min(monitor.x - 40.0).max(760.0), 238.0);
                let position = egui::pos2(((monitor.x - size.x) / 2.0).max(18.0), 64.0);
                (
                    size,
                    egui::vec2(720.0, 190.0),
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
        ui.add_space(6.0);
        ui::compact_overlay_frame().show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                let drag = ui.add(
                    egui::Label::new(RichText::new("REC Sidecar").size(15.0).strong())
                        .sense(egui::Sense::click_and_drag()),
                );
                if drag.drag_started() || drag.dragged() {
                    ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
                }
                ui.label(
                    RichText::new(self.app_state_label())
                        .size(12.0)
                        .color(self.app_state_color()),
                );

                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Развернуть").clicked() {
                        self.expanded_workspace = true;
                        self.applied_window_mode = None;
                    }
                    if ui.button("Помоги").clicked() {
                        self.send_help_request();
                    }
                    let pause_label = if self.connecting || self.recording {
                        "Стоп"
                    } else {
                        "Продолжить"
                    };
                    if ui.button(pause_label).clicked() {
                        if self.connecting || self.recording {
                            self.pause_recording();
                        } else {
                            self.continue_recording();
                        }
                    }
                });
            });

            ui.separator();
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.set_width(164.0);
                    ui.label(
                        RichText::new(self.transcript_state_label())
                            .size(12.0)
                            .color(egui::Color32::from_rgb(168, 180, 192)),
                    );
                    let last_line = self
                        .live_partial
                        .as_deref()
                        .or_else(|| self.bubbles.last().map(String::as_str))
                        .unwrap_or("Жду речь...");
                    ui.add(egui::Label::new(RichText::new(last_line).size(13.0)).wrap());
                });

                ui.separator();
                ui.vertical(|ui| {
                    ui.set_width((ui.available_width() - 188.0).max(360.0));
                    self.show_compact_stage_summary(ui);
                });

                ui.separator();
                ui.vertical(|ui| {
                    ui.set_width(164.0);
                    ui.label(
                        RichText::new(self.coach_state_label())
                            .size(12.0)
                            .color(egui::Color32::from_rgb(168, 180, 192)),
                    );
                    let help_busy = self.help_is_busy();
                    let helper_text = if help_busy {
                        "готовлю подсказку..."
                    } else {
                        "готов к подсказке"
                    };
                    ui.label(RichText::new(helper_text).size(13.0));
                    if ui.button("Сохранить").clicked() {
                        if let Err(err) = self.save_current_run() {
                            self.status = format!("Save failed: {}", err);
                        }
                    }
                });
            });
        });
    }

    fn show_compact_stage_summary(&mut self, ui: &mut egui::Ui) {
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
                let label = agenda
                    .scorecard
                    .as_ref()
                    .map(|scorecard| {
                        format!(
                            "{} · {} · {}/{}",
                            agenda.stage,
                            scorecard.readiness_label,
                            scorecard.hit_count,
                            scorecard.total_count
                        )
                    })
                    .unwrap_or_else(|| format!("{} · {}", agenda.stage, agenda.title));
                ui.label(
                    RichText::new(label)
                        .size(12.0)
                        .strong()
                        .color(readiness_color(readiness_key)),
                );
                let advice = agenda
                    .scorecard
                    .as_ref()
                    .map(|scorecard| scorecard.next_action.as_str())
                    .unwrap_or(agenda.step.as_str());
                ui.add(
                    egui::Label::new(
                        RichText::new(advice)
                            .size(17.0)
                            .strong()
                            .color(readiness_text_color(readiness_key)),
                    )
                    .wrap(),
                );
            } else {
                ui.label(
                    RichText::new("Определяю стадию")
                        .size(12.0)
                        .strong()
                        .color(readiness_color("pending")),
                );
                ui.add(
                    egui::Label::new(
                        RichText::new("Говорите, я слушаю и собираю контекст.")
                            .size(17.0)
                            .strong()
                            .color(readiness_text_color("pending")),
                    )
                    .wrap(),
                );
            }
        });
    }

    fn show_top_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("REC Sidecar").size(18.0).strong());
            ui.add_space(10.0);
            self.show_primary_controls(ui);
            if self.current_run.is_some() && ui.button("Компактно").clicked() {
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

    fn show_primary_controls(&mut self, ui: &mut egui::Ui) {
        let primary_label = if self.connecting {
            "Отменить"
        } else if self.recording {
            "Остановить"
        } else if self.current_run.is_some() {
            "Продолжить"
        } else {
            "Начать REC"
        };

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

        if ui
            .add_enabled(!self.connecting, egui::Button::new("Новый"))
            .clicked()
        {
            self.start_new_recording();
        }

        if ui
            .add_enabled(self.current_run.is_some(), egui::Button::new("Сохранить"))
            .clicked()
        {
            if let Err(err) = self.save_current_run() {
                self.status = format!("Save failed: {}", err);
            }
        }

        if ui
            .add_enabled(
                self.current_run.is_some(),
                egui::Button::new("Сохранить и закрыть"),
            )
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
        if ui
            .add_enabled(
                self.current_run.is_some() && !help_busy,
                egui::Button::new(help_label),
            )
            .clicked()
        {
            self.send_help_request();
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

    fn show_idle_dashboard(&mut self, ui: &mut egui::Ui) {
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
            self.show_primary_controls(ui);
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
            if ui.button("Посмотреть").clicked() {
                self.view_selected_history();
            }

            if ui.button("Выгрузить").clicked() {
                self.export_selected_history();
            }

            if ui.button("Обновить").clicked() {
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
                            RichText::new(&scorecard.next_action)
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

        if COACH_AUTO_SUGGESTIONS {
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

fn stage_detect_interval() -> Duration {
    Duration::from_millis(env_u64(
        "COACH_STAGE_DETECT_INTERVAL_MS",
        DEFAULT_STAGE_DETECT_INTERVAL_MS,
    ))
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

fn env_flag(name: &str) -> bool {
    env_var(name)
        .map(|value| {
            matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
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

    fn run_headless_frame(app: &mut RecApp) {
        let ctx = egui::Context::default();
        let mut frame = eframe::Frame::_new_kittest();
        let _ = ctx.run(egui::RawInput::default(), |ctx| {
            app.update(ctx, &mut frame);
        });
    }

    #[test]
    fn default_app_renders_dashboard_headlessly() {
        let (_dir, paths) = temp_paths();
        let mut app = RecApp::new_with_paths(paths);

        run_headless_frame(&mut app);

        assert_eq!(app.status, "Ready");
        assert!(!app.recording);
        assert!(app.history.is_empty());
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

        run_headless_frame(&mut app);

        assert!(app.help_is_busy());
    }
}
