mod api;
mod audio;
#[allow(dead_code)]
#[path = "../../../src/audio_aec.rs"]
mod audio_aec;
mod logging;
mod models;
mod product;
mod runtime;
mod sse;
mod storage;

use models::{
    AudioConfigPatch, AudioKind, AudioSnapshot, AuthUser, SessionEnvelope, SessionEventRequest,
};
use runtime::AppCore;
use std::sync::Arc;
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};

#[tauri::command]
async fn auth_status(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
) -> Result<Option<AuthUser>, String> {
    core.inner().auth_status(&app).await
}

#[tauri::command]
async fn auth_login(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    email: String,
    password: String,
) -> Result<AuthUser, String> {
    core.inner()
        .authenticate(&app, email.trim(), &password, false)
        .await
}

#[tauri::command]
async fn auth_register(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    email: String,
    password: String,
) -> Result<AuthUser, String> {
    core.inner()
        .authenticate(&app, email.trim(), &password, true)
        .await
}

#[tauri::command]
async fn auth_logout(app: tauri::AppHandle, core: State<'_, Arc<AppCore>>) -> Result<(), String> {
    core.inner().logout(&app).await
}

#[tauri::command]
async fn session_resume_or_create(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
) -> Result<SessionEnvelope, String> {
    core.inner().resume_or_create(&app).await
}

#[tauri::command]
fn session_current(core: State<'_, Arc<AppCore>>) -> Option<SessionEnvelope> {
    core.current_session()
}

#[tauri::command]
async fn session_create(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
) -> Result<SessionEnvelope, String> {
    core.inner().create_session(&app).await
}

#[tauri::command]
async fn session_post_event(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    event: SessionEventRequest,
) -> Result<(), String> {
    let (token, session_id) = core.token_and_session().map_err(|error| {
        tracing::warn!(%error, "session event rejected before request");
        error
    })?;
    core.api
        .post_event(&token, &session_id, &event)
        .await
        .map_err(|error| {
            if error.unauthorized() {
                core.expire_auth(&app);
            }
            error.to_string()
        })
}

#[tauri::command]
fn audio_start(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    kind: AudioKind,
) -> Result<AudioSnapshot, String> {
    let (token, session_id) = core.token_and_session().map_err(|error| {
        tracing::warn!(?kind, %error, "audio start rejected before capture");
        error
    })?;
    let api = core.api.clone();
    core.audio
        .lock()
        .expect("audio manager")
        .start(kind, &session_id, &token, api, app)
}

#[tauri::command]
fn audio_stop(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    kind: AudioKind,
) -> AudioSnapshot {
    core.audio
        .lock()
        .expect("audio manager")
        .stop(kind, Some(&app))
}

#[tauri::command]
fn audio_configure(
    app: tauri::AppHandle,
    core: State<'_, Arc<AppCore>>,
    config: AudioConfigPatch,
) -> AudioSnapshot {
    core.audio
        .lock()
        .expect("audio manager")
        .configure(config, &app)
}

#[tauri::command]
fn diagnostics_log_path() -> Result<String, String> {
    logging::log_path().map(|path| path.display().to_string())
}

#[tauri::command]
fn reply_window_open(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("reply") {
        window.show().map_err(|error| error.to_string())?;
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(());
    }
    WebviewWindowBuilder::new(&app, "reply", WebviewUrl::App("pip.html".into()))
        .title("Предлагаемая реплика")
        .inner_size(560.0, 380.0)
        .min_inner_size(420.0, 300.0)
        .always_on_top(true)
        .resizable(true)
        .build()
        .map_err(|error| error.to_string())?;
    Ok(())
}

pub fn run() {
    let _log_guard = logging::init().ok();
    tracing::info!(
        version = env!("CARGO_PKG_VERSION"),
        product = crate::product::PRODUCT_NAME,
        "desktop starting"
    );
    let core = AppCore::new().expect("failed to initialize desktop runtime");
    tauri::Builder::default()
        .manage(core)
        .invoke_handler(tauri::generate_handler![
            auth_status,
            auth_login,
            auth_register,
            auth_logout,
            session_resume_or_create,
            session_current,
            session_create,
            session_post_event,
            audio_start,
            audio_stop,
            audio_configure,
            diagnostics_log_path,
            reply_window_open,
        ])
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                let core = window.state::<Arc<AppCore>>();
                core.audio
                    .lock()
                    .expect("audio manager")
                    .stop(AudioKind::All, Some(window.app_handle()));
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running desktop application");
}
