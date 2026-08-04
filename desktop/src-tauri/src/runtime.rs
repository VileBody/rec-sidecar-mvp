use crate::{
    api::{ApiClient, ApiError},
    audio::AudioManager,
    models::{AuthUser, ConnectionStatus, SessionEnvelope, SessionState},
    sse::SseDecoder,
    storage::{CachedSession, DesktopStorage},
};
use futures_util::StreamExt;
use std::sync::{Arc, Mutex, RwLock};
use tauri::{AppHandle, Emitter, Manager};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
struct AuthContext {
    token: String,
    user: AuthUser,
}

struct SessionRuntime {
    id: String,
    snapshot: SessionState,
    cancel: CancellationToken,
}

pub struct AppCore {
    pub api: ApiClient,
    storage: DesktopStorage,
    auth: RwLock<Option<AuthContext>>,
    session: RwLock<Option<SessionRuntime>>,
    pub audio: Mutex<AudioManager>,
}

impl AppCore {
    pub fn new() -> Result<Arc<Self>, String> {
        tracing::info!(
            backend = crate::api::PRODUCTION_BASE_URL,
            "runtime initialized"
        );
        Ok(Arc::new(Self {
            api: ApiClient::production().map_err(|error| error.to_string())?,
            storage: DesktopStorage::new().map_err(|error| error.to_string())?,
            auth: RwLock::new(None),
            session: RwLock::new(None),
            audio: Mutex::new(AudioManager::new()?),
        }))
    }

    pub async fn auth_status(
        self: &Arc<Self>,
        app: &AppHandle,
    ) -> Result<Option<AuthUser>, String> {
        tracing::info!("checking stored authorization");
        if let Some(auth) = self.auth.read().expect("auth state").as_ref() {
            tracing::info!(user_id = %auth.user.id, "authorization already loaded");
            return Ok(Some(auth.user.clone()));
        }
        let token = match self.storage.load_token() {
            Ok(Some(token)) => {
                tracing::info!("stored authorization token found");
                token
            }
            Ok(None) => {
                tracing::info!("stored authorization token not found");
                return Ok(None);
            }
            Err(error) => {
                tracing::error!(%error, "failed to read authorization from Keychain");
                return Err(error);
            }
        };
        match self.api.me(&token).await {
            Ok(user) if user.role == crate::product::ACCOUNT_ROLE => {
                *self.auth.write().expect("auth state") = Some(AuthContext {
                    token,
                    user: user.clone(),
                });
                let _ = app.emit("auth://state", Some(&user));
                tracing::info!(user_id = %user.id, role = %user.role, "authorization restored");
                Ok(Some(user))
            }
            Ok(user) => {
                let _ = self.storage.clear_token();
                Err(crate::product::unsupported_role_message(&user.role))
            }
            Err(error) if error.unauthorized() => {
                tracing::warn!("stored authorization expired");
                self.expire_auth(app);
                Ok(None)
            }
            Err(error) => {
                tracing::error!(%error, "authorization restore request failed");
                Err(error.to_string())
            }
        }
    }

    pub async fn authenticate(
        self: &Arc<Self>,
        app: &AppHandle,
        email: &str,
        password: &str,
        register: bool,
    ) -> Result<AuthUser, String> {
        tracing::info!(register, "authorization request started");
        let response = if register {
            self.api.register(email, password).await
        } else {
            self.api.login(email, password).await
        }
        .map_err(|error| {
            tracing::warn!(register, %error, "authorization request failed");
            error.to_string()
        })?;
        if response.user.role != crate::product::ACCOUNT_ROLE {
            let _ = self.api.logout(&response.token).await;
            return Err(crate::product::unsupported_role_message(
                &response.user.role,
            ));
        }
        self.storage.save_token(&response.token)?;
        *self.auth.write().expect("auth state") = Some(AuthContext {
            token: response.token,
            user: response.user.clone(),
        });
        let _ = app.emit("auth://state", Some(&response.user));
        tracing::info!(
            user_id = %response.user.id,
            role = %response.user.role,
            register,
            "authorization succeeded"
        );
        Ok(response.user)
    }

    pub async fn logout(self: &Arc<Self>, app: &AppHandle) -> Result<(), String> {
        tracing::info!("logout requested");
        self.stop_active_session(app, true);
        let token = self
            .auth
            .write()
            .expect("auth state")
            .take()
            .map(|auth| auth.token)
            .or_else(|| self.storage.load_token().ok().flatten());
        if let Some(token) = token {
            let _ = self.api.logout(&token).await;
        }
        self.storage.clear_token()?;
        self.storage
            .clear_session()
            .map_err(|error| error.to_string())?;
        if let Some(reply) = app.get_webview_window("reply") {
            let _ = reply.close();
        }
        let _ = app.emit::<Option<AuthUser>>("auth://state", None);
        Ok(())
    }

    pub async fn resume_or_create(
        self: &Arc<Self>,
        app: &AppHandle,
    ) -> Result<SessionEnvelope, String> {
        let token = self.token()?;
        if let Ok(Some(cached)) = self.storage.load_session() {
            if !cached.session_id.is_empty() {
                match self.api.get_session(&token, &cached.session_id).await {
                    Ok(snapshot) => {
                        return self.activate_session(app, cached.session_id, snapshot);
                    }
                    Err(error) if error.unauthorized() => {
                        self.expire_auth(app);
                        return Err("нужно войти".to_string());
                    }
                    Err(error)
                        if error.not_found()
                            || error.status == Some(reqwest::StatusCode::FORBIDDEN) =>
                    {
                        let _ = self.storage.clear_session();
                    }
                    Err(_) => {
                        if let Some(snapshot) = cached.snapshot {
                            return self.activate_session(app, cached.session_id, snapshot);
                        }
                    }
                }
            }
        }
        match self.api.latest_session(&token).await {
            Ok(envelope) => self.activate_session(app, envelope.session_id, envelope.state),
            Err(error) if error.not_found() => self.create_session(app).await,
            Err(error) if error.unauthorized() => {
                self.expire_auth(app);
                Err("нужно войти".to_string())
            }
            Err(error) => {
                tracing::error!(%error, "latest session request failed");
                Err(error.to_string())
            }
        }
    }

    pub async fn create_session(
        self: &Arc<Self>,
        app: &AppHandle,
    ) -> Result<SessionEnvelope, String> {
        let token = self.token()?;
        tracing::info!("session creation requested");
        let envelope = self.api.create_session(&token).await.map_err(|error| {
            if error.unauthorized() {
                self.expire_auth(app);
            }
            tracing::error!(%error, "session creation failed");
            error.to_string()
        })?;
        self.activate_session(app, envelope.session_id, envelope.state)
    }

    pub fn current_session(&self) -> Option<SessionEnvelope> {
        self.session
            .read()
            .expect("session state")
            .as_ref()
            .map(|runtime| SessionEnvelope {
                session_id: runtime.id.clone(),
                state: runtime.snapshot.clone(),
            })
    }

    pub fn token_and_session(&self) -> Result<(String, String), String> {
        let token = self.token()?;
        let session_id = self
            .session
            .read()
            .expect("session state")
            .as_ref()
            .map(|runtime| runtime.id.clone())
            .ok_or_else(|| "сессия еще не создана".to_string())?;
        Ok((token, session_id))
    }

    fn token(&self) -> Result<String, String> {
        self.auth
            .read()
            .expect("auth state")
            .as_ref()
            .map(|auth| auth.token.clone())
            .ok_or_else(|| "нужно войти".to_string())
    }

    fn activate_session(
        self: &Arc<Self>,
        app: &AppHandle,
        session_id: String,
        mut snapshot: SessionState,
    ) -> Result<SessionEnvelope, String> {
        self.stop_active_session(app, false);
        if snapshot.session_id.is_empty() {
            snapshot.session_id = session_id.clone();
        }
        let cancel = CancellationToken::new();
        *self.session.write().expect("session state") = Some(SessionRuntime {
            id: session_id.clone(),
            snapshot: snapshot.clone(),
            cancel: cancel.clone(),
        });
        self.storage
            .save_session(&CachedSession {
                session_id: session_id.clone(),
                snapshot: Some(snapshot.clone()),
            })
            .map_err(|error| error.to_string())?;
        let envelope = SessionEnvelope {
            session_id: session_id.clone(),
            state: snapshot,
        };
        let _ = app.emit("session://snapshot", &envelope);
        tracing::info!(session_id = %session_id, "session activated");
        self.spawn_session_tasks(app.clone(), session_id, cancel);
        Ok(envelope)
    }

    fn stop_active_session(&self, app: &AppHandle, clear: bool) {
        if let Some(runtime) = self.session.write().expect("session state").take() {
            tracing::info!(session_id = %runtime.id, clear, "session stopped");
            runtime.cancel.cancel();
        }
        self.audio
            .lock()
            .expect("audio manager")
            .stop(crate::models::AudioKind::All, Some(app));
        if clear {
            let _ = self.storage.clear_session();
        }
    }

    pub fn expire_auth(&self, app: &AppHandle) {
        tracing::warn!("authorization cleared after unauthorized response");
        if let Some(runtime) = self.session.write().expect("session state").take() {
            runtime.cancel.cancel();
        }
        self.audio
            .lock()
            .expect("audio manager")
            .stop(crate::models::AudioKind::All, Some(app));
        *self.auth.write().expect("auth state") = None;
        let _ = self.storage.clear_token();
        let _ = self.storage.clear_session();
        let _ = app.emit::<Option<AuthUser>>("auth://state", None);
    }

    fn apply_snapshot(&self, app: &AppHandle, session_id: &str, mut snapshot: SessionState) {
        let mut session = self.session.write().expect("session state");
        let Some(runtime) = session.as_mut().filter(|runtime| runtime.id == session_id) else {
            return;
        };
        if snapshot.session_id.is_empty() {
            snapshot.session_id = session_id.to_string();
        }
        runtime.snapshot = snapshot.clone();
        drop(session);
        let envelope = SessionEnvelope {
            session_id: session_id.to_string(),
            state: snapshot.clone(),
        };
        let _ = self.storage.save_session(&CachedSession {
            session_id: session_id.to_string(),
            snapshot: Some(snapshot),
        });
        let _ = app.emit("session://snapshot", envelope);
    }

    fn connection_status(app: &AppHandle, state: &str, detail: impl Into<String>) {
        let _ = app.emit(
            "connection://status",
            ConnectionStatus {
                state: state.to_string(),
                detail: detail.into(),
            },
        );
    }

    fn spawn_session_tasks(
        self: &Arc<Self>,
        app: AppHandle,
        session_id: String,
        cancel: CancellationToken,
    ) {
        let sse_core = Arc::clone(self);
        let sse_app = app.clone();
        let sse_session = session_id.clone();
        let sse_cancel = cancel.clone();
        tauri::async_runtime::spawn(async move {
            sse_loop(sse_core, sse_app, sse_session, sse_cancel).await;
        });

        let poll_core = Arc::clone(self);
        tauri::async_runtime::spawn(async move {
            polling_loop(poll_core, app, session_id, cancel).await;
        });
    }
}

async fn sse_loop(
    core: Arc<AppCore>,
    app: AppHandle,
    session_id: String,
    cancel: CancellationToken,
) {
    let mut reconnect_delay = 500_u64;
    loop {
        if cancel.is_cancelled() {
            return;
        }
        let token = match core.token() {
            Ok(token) => token,
            Err(_) => return,
        };
        AppCore::connection_status(&app, "connecting", "подключаю live stream");
        match core.api.session_stream(&token, &session_id).await {
            Ok(response) => {
                reconnect_delay = 500;
                AppCore::connection_status(&app, "streaming", "streaming");
                let mut bytes = response.bytes_stream();
                let mut decoder = SseDecoder::default();
                loop {
                    tokio::select! {
                        _ = cancel.cancelled() => return,
                        chunk = bytes.next() => match chunk {
                            Some(Ok(chunk)) => {
                                for snapshot in decoder.push(&chunk) {
                                    core.apply_snapshot(&app, &session_id, snapshot);
                                }
                            }
                            Some(Err(_)) | None => break,
                        }
                    }
                }
            }
            Err(error) if error.unauthorized() => {
                core.expire_auth(&app);
                return;
            }
            Err(error) => {
                tracing::warn!(session_id = %session_id, error = %error, "SSE connection failed");
                AppCore::connection_status(&app, "reconnecting", error.to_string())
            }
        }
        AppCore::connection_status(&app, "reconnecting", "live stream переподключается");
        tokio::select! {
            _ = cancel.cancelled() => return,
            _ = tokio::time::sleep(std::time::Duration::from_millis(reconnect_delay)) => {}
        }
        reconnect_delay = (reconnect_delay as f64 * 1.6).min(5_000.0) as u64;
    }
}

async fn polling_loop(
    core: Arc<AppCore>,
    app: AppHandle,
    session_id: String,
    cancel: CancellationToken,
) {
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(2));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    interval.tick().await;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => return,
            _ = interval.tick() => {
                let token = match core.token() {
                    Ok(token) => token,
                    Err(_) => return,
                };
                match core.api.get_session(&token, &session_id).await {
                    Ok(snapshot) => core.apply_snapshot(&app, &session_id, snapshot),
                    Err(error) if error.unauthorized() => {
                        core.expire_auth(&app);
                        return;
                    }
                    Err(_) => {}
                }
            }
        }
    }
}

#[allow(dead_code)]
pub fn api_error_to_string(error: ApiError) -> String {
    error.to_string()
}
