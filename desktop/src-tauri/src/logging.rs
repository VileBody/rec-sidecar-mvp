use directories::ProjectDirs;
use std::{
    fs::{self, OpenOptions},
    path::PathBuf,
};
use tracing::Level;
use tracing_appender::non_blocking::WorkerGuard;

pub fn log_path() -> Result<PathBuf, String> {
    let dirs = ProjectDirs::from("ru", "TeamGenius", crate::product::APP_SUPPORT_NAME)
        .ok_or_else(|| "application support directory is unavailable".to_string())?;
    Ok(dirs
        .data_local_dir()
        .join("logs")
        .join(crate::product::LOG_FILE_NAME))
}

pub fn init() -> Result<WorkerGuard, String> {
    let path = log_path()?;
    let parent = path
        .parent()
        .ok_or_else(|| "invalid log path".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|error| error.to_string())?;
    let (writer, guard) = tracing_appender::non_blocking(file);
    tracing_subscriber::fmt()
        .with_ansi(false)
        .with_max_level(Level::INFO)
        .with_target(false)
        .with_writer(writer)
        .try_init()
        .map_err(|error| error.to_string())?;
    Ok(guard)
}
