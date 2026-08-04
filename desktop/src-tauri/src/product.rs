#[cfg(feature = "personal")]
pub const PRODUCT_NAME: &str = "REC Personal";
#[cfg(not(feature = "personal"))]
pub const PRODUCT_NAME: &str = "REC Coach";

#[cfg(feature = "personal")]
pub const ACCOUNT_ROLE: &str = "personal";
#[cfg(not(feature = "personal"))]
pub const ACCOUNT_ROLE: &str = "sales";

#[cfg(feature = "personal")]
pub const AUTO_OPENER: bool = false;
#[cfg(not(feature = "personal"))]
pub const AUTO_OPENER: bool = true;

#[cfg(feature = "personal")]
pub const KEYCHAIN_SERVICE: &str = "ru.teamgenius.rec-personal";
#[cfg(not(feature = "personal"))]
pub const KEYCHAIN_SERVICE: &str = "ru.teamgenius.rec-coach";

#[cfg(feature = "personal")]
pub const APP_SUPPORT_NAME: &str = "REC Personal";
#[cfg(not(feature = "personal"))]
pub const APP_SUPPORT_NAME: &str = "REC Coach";

#[cfg(feature = "personal")]
pub const LOG_FILE_NAME: &str = "rec-personal.log";
#[cfg(not(feature = "personal"))]
pub const LOG_FILE_NAME: &str = "rec-coach.log";

#[cfg(feature = "personal")]
pub const USER_AGENT: &str = "REC-Personal-Desktop/0.1";
#[cfg(not(feature = "personal"))]
pub const USER_AGENT: &str = "REC-Coach-Desktop/0.1";

pub fn unsupported_role_message(role: &str) -> String {
    format!(
        "{} поддерживает только аккаунты {}; роль этого аккаунта: {}",
        PRODUCT_NAME, ACCOUNT_ROLE, role
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn product_settings_are_internally_consistent() {
        if cfg!(feature = "personal") {
            assert_eq!(ACCOUNT_ROLE, "personal");
            assert!(KEYCHAIN_SERVICE.contains("personal"));
        } else {
            assert_eq!(ACCOUNT_ROLE, "sales");
            assert!(KEYCHAIN_SERVICE.contains("coach"));
        }
    }
}
