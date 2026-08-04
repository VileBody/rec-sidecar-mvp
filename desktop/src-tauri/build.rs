use std::{env, path::PathBuf};

const COMMANDS: &[&str] = &[
    "auth_status",
    "auth_login",
    "auth_register",
    "auth_logout",
    "session_resume_or_create",
    "session_current",
    "session_create",
    "session_post_event",
    "audio_start",
    "audio_stop",
    "audio_configure",
    "diagnostics_log_path",
    "reply_window_open",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build Tauri context");

    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let native_source = manifest_dir.join("../../native/system_audio_tap.m");
    println!("cargo:rerun-if-changed={}", native_source.display());
    cc::Build::new()
        .file(native_source)
        .flag("-fobjc-arc")
        .compile("rec_coach_system_audio_tap");
    println!("cargo:rustc-link-lib=framework=Foundation");
    println!("cargo:rustc-link-lib=framework=CoreAudio");
}
