pub mod app;
pub mod asr;
pub mod coach;
pub mod context;
pub mod session;
pub mod ui;

pub fn run() -> eframe::Result {
    let _ = dotenvy::dotenv();

    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("REC Sidecar")
            .with_inner_size([1320.0, 820.0])
            .with_min_inner_size([960.0, 620.0]),
        ..Default::default()
    };

    eframe::run_native(
        "REC Sidecar",
        options,
        Box::new(|_cc| Ok(Box::new(app::RecApp::new()))),
    )
}
