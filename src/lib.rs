pub mod app;
pub mod asr;
pub mod audio_aec;
pub mod coach;
pub mod context;
pub mod session;
pub mod ui;

pub fn run() -> eframe::Result {
    let _ = dotenvy::dotenv();
    let auto_start = std::env::var("REC_AUTO_START")
        .ok()
        .map(|value| {
            matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false);
    let initial_size = if auto_start {
        [980.0, 300.0]
    } else {
        [1320.0, 820.0]
    };
    let min_size = if auto_start {
        [460.0, 300.0]
    } else {
        [960.0, 620.0]
    };

    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("REC Sidecar")
            .with_inner_size(initial_size)
            .with_min_inner_size(min_size)
            .with_transparent(true)
            .with_decorations(false)
            .with_resizable(true),
        ..Default::default()
    };

    eframe::run_native(
        "REC Sidecar",
        options,
        Box::new(|_cc| Ok(Box::new(app::RecApp::new()))),
    )
}
