use chrono::{DateTime, Local};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

const RUNS_DIR: &str = "runs";
const EXPORTS_DIR: &str = "exports";

#[derive(Clone, Debug)]
pub struct AppPaths {
    runs_dir: PathBuf,
    exports_dir: PathBuf,
}

impl AppPaths {
    pub fn new(runs_dir: impl Into<PathBuf>, exports_dir: impl Into<PathBuf>) -> Self {
        Self {
            runs_dir: runs_dir.into(),
            exports_dir: exports_dir.into(),
        }
    }

    pub(crate) fn runs_dir(&self) -> &Path {
        &self.runs_dir
    }

    pub(crate) fn exports_dir(&self) -> &Path {
        &self.exports_dir
    }
}

impl Default for AppPaths {
    fn default() -> Self {
        Self::new(RUNS_DIR, EXPORTS_DIR)
    }
}

#[derive(Clone)]
pub(crate) struct SavedRun {
    pub(crate) title: String,
    pub(crate) modified: String,
    pub(crate) path: PathBuf,
}

pub(crate) struct RunSession {
    pub(crate) id: String,
    pub(crate) title: String,
    pub(crate) path: Option<PathBuf>,
}

impl RunSession {
    pub(crate) fn new() -> Self {
        let now = Local::now();

        Self {
            id: now.format("%Y%m%d-%H%M%S").to_string(),
            title: format!("Run {}", now.format("%Y-%m-%d %H:%M:%S")),
            path: None,
        }
    }
}

pub(crate) fn save_run(
    paths: &AppPaths,
    session: &mut RunSession,
    content: &str,
) -> io::Result<PathBuf> {
    fs::create_dir_all(paths.runs_dir())?;
    let path = session
        .path
        .clone()
        .unwrap_or_else(|| paths.runs_dir().join(format!("{}.txt", session.id)));

    fs::write(&path, content)?;
    session.path = Some(path.clone());
    Ok(path)
}

pub(crate) fn read_run_text(run: &SavedRun) -> io::Result<String> {
    fs::read_to_string(&run.path)
}

pub(crate) fn export_run(paths: &AppPaths, run: &SavedRun) -> io::Result<PathBuf> {
    fs::create_dir_all(paths.exports_dir())?;
    let filename = run
        .path
        .file_name()
        .map(|name| name.to_owned())
        .unwrap_or_else(|| "transcript.txt".into());
    let target = paths.exports_dir().join(filename);
    fs::copy(&run.path, &target)?;
    Ok(target)
}

pub(crate) fn read_saved_runs(paths: &AppPaths) -> Vec<SavedRun> {
    let Ok(entries) = fs::read_dir(paths.runs_dir()) else {
        return Vec::new();
    };

    let mut runs = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "txt"))
        .filter_map(|path| saved_run_from_path(path).ok())
        .collect::<Vec<_>>();

    runs.sort_by(|left, right| right.modified.cmp(&left.modified));
    runs
}

pub(crate) fn saved_run_from_path(path: PathBuf) -> io::Result<SavedRun> {
    let title = fs::read_to_string(&path)
        .ok()
        .and_then(|text| {
            text.lines()
                .find_map(|line| line.strip_prefix("Title: ").map(str::to_string))
        })
        .or_else(|| {
            path.file_stem()
                .and_then(|stem| stem.to_str())
                .map(str::to_string)
        })
        .unwrap_or_else(|| "Untitled run".to_string());

    let modified = fs::metadata(&path)
        .and_then(|metadata| metadata.modified())
        .map(|time| {
            let datetime: DateTime<Local> = time.into();
            datetime.format("%Y-%m-%d %H:%M:%S").to_string()
        })
        .unwrap_or_else(|_| "unknown time".to_string());

    Ok(SavedRun {
        title,
        modified,
        path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_run_title_prefers_title_header() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run.txt");
        fs::write(&path, "Title: Discovery Call\n\n--- Transcript ---\n").unwrap();

        let run = saved_run_from_path(path.clone()).unwrap();

        assert_eq!(run.title, "Discovery Call");
        assert_eq!(run.path, path);
    }

    #[test]
    fn save_and_export_run_use_injected_paths() {
        let dir = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(dir.path().join("runs"), dir.path().join("exports"));
        let mut session = RunSession {
            id: "test-run".to_string(),
            title: "Test Run".to_string(),
            path: None,
        };

        let saved = save_run(&paths, &mut session, "hello").unwrap();
        let run = saved_run_from_path(saved.clone()).unwrap();
        let exported = export_run(&paths, &run).unwrap();

        assert_eq!(saved, paths.runs_dir().join("test-run.txt"));
        assert_eq!(exported, paths.exports_dir().join("test-run.txt"));
        assert_eq!(fs::read_to_string(exported).unwrap(), "hello");
    }
}
