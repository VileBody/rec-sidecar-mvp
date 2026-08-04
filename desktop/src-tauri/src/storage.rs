use crate::models::SessionState;
use directories::ProjectDirs;
use keyring::Entry;
use serde::{Deserialize, Serialize};
use std::{fs, io, path::PathBuf};

const KEYCHAIN_ACCOUNT: &str = "production-auth-token";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CachedSession {
    pub session_id: String,
    pub snapshot: Option<SessionState>,
}

#[derive(Debug, Clone)]
pub struct DesktopStorage {
    state_path: PathBuf,
}

impl DesktopStorage {
    pub fn new() -> io::Result<Self> {
        let dirs = ProjectDirs::from("ru", "TeamGenius", crate::product::APP_SUPPORT_NAME)
            .ok_or_else(|| io::Error::other("application support directory is unavailable"))?;
        let data_dir = dirs.data_local_dir();
        fs::create_dir_all(data_dir)?;
        Ok(Self {
            state_path: data_dir.join("session-state.json"),
        })
    }

    #[cfg(test)]
    pub fn with_state_path(state_path: PathBuf) -> Self {
        Self { state_path }
    }

    pub fn load_token(&self) -> Result<Option<String>, String> {
        let entry = Entry::new(crate::product::KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            .map_err(|e| e.to_string())?;
        match entry.get_password() {
            Ok(token) if !token.trim().is_empty() => Ok(Some(token)),
            Ok(_) | Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(error.to_string()),
        }
    }

    pub fn save_token(&self, token: &str) -> Result<(), String> {
        Entry::new(crate::product::KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            .map_err(|e| e.to_string())?
            .set_password(token)
            .map_err(|e| e.to_string())
    }

    pub fn clear_token(&self) -> Result<(), String> {
        let entry = Entry::new(crate::product::KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            .map_err(|e| e.to_string())?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(error.to_string()),
        }
    }

    pub fn load_session(&self) -> io::Result<Option<CachedSession>> {
        match fs::read(&self.state_path) {
            Ok(raw) => serde_json::from_slice(&raw)
                .map(Some)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub fn save_session(&self, session: &CachedSession) -> io::Result<()> {
        let parent = self
            .state_path
            .parent()
            .ok_or_else(|| io::Error::other("invalid state path"))?;
        fs::create_dir_all(parent)?;
        let temporary = self.state_path.with_extension("json.tmp");
        fs::write(&temporary, serde_json::to_vec(session)?)?;
        fs::rename(temporary, &self.state_path)
    }

    pub fn clear_session(&self) -> io::Result<()> {
        match fs::remove_file(&self.state_path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_cache_round_trip_and_clear() {
        let dir = tempfile::tempdir().unwrap();
        let storage = DesktopStorage::with_state_path(dir.path().join("state.json"));
        let expected = CachedSession {
            session_id: "sess-42".to_string(),
            snapshot: Some(SessionState {
                seller_draft: "hello".to_string(),
                ..SessionState::default()
            }),
        };
        storage.save_session(&expected).unwrap();
        let loaded = storage.load_session().unwrap().unwrap();
        assert_eq!(loaded.session_id, "sess-42");
        assert_eq!(loaded.snapshot.unwrap().seller_draft, "hello");
        storage.clear_session().unwrap();
        assert!(storage.load_session().unwrap().is_none());
    }
}
