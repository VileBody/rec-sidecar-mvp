use crate::models::{
    AuthResponse, AuthUser, MeResponse, SessionEnvelope, SessionEventRequest, SessionState,
};
use reqwest::{Client, Response, StatusCode};
use serde::Serialize;
use std::{fmt, time::Duration};
use url::Url;

pub const PRODUCTION_BASE_URL: &str = "https://rec.teamgenius.ru";

#[derive(Debug, Clone)]
pub struct ApiError {
    pub status: Option<StatusCode>,
    pub message: String,
}

impl ApiError {
    pub fn unauthorized(&self) -> bool {
        self.status == Some(StatusCode::UNAUTHORIZED)
    }

    pub fn not_found(&self) -> bool {
        self.status == Some(StatusCode::NOT_FOUND)
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for ApiError {}

impl From<reqwest::Error> for ApiError {
    fn from(error: reqwest::Error) -> Self {
        Self {
            status: error.status(),
            message: error.to_string(),
        }
    }
}

#[derive(Clone)]
pub struct ApiClient {
    client: Client,
    base_url: Url,
}

impl ApiClient {
    pub fn production() -> Result<Self, ApiError> {
        Self::new(PRODUCTION_BASE_URL)
    }

    pub fn new(base_url: &str) -> Result<Self, ApiError> {
        let base_url = Url::parse(base_url).map_err(|error| ApiError {
            status: None,
            message: format!("invalid backend URL: {error}"),
        })?;
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .user_agent(crate::product::USER_AGENT)
            .build()?;
        Ok(Self { client, base_url })
    }

    pub fn websocket_url(&self, path: &str) -> Result<Url, ApiError> {
        let mut url = self.url(path)?;
        match url.scheme() {
            "https" => url.set_scheme("wss"),
            "http" => url.set_scheme("ws"),
            _ => Err(()),
        }
        .map_err(|_| ApiError {
            status: None,
            message: "backend URL cannot be converted to WebSocket URL".to_string(),
        })?;
        Ok(url)
    }

    pub async fn login(&self, email: &str, password: &str) -> Result<AuthResponse, ApiError> {
        self.auth_request(
            "/v1/auth/login",
            &serde_json::json!({"email": email, "password": password}),
        )
        .await
    }

    pub async fn register(&self, email: &str, password: &str) -> Result<AuthResponse, ApiError> {
        self.auth_request(
            "/v1/auth/register",
            &serde_json::json!({"email": email, "password": password, "role": crate::product::ACCOUNT_ROLE}),
        )
        .await
    }

    async fn auth_request(
        &self,
        path: &str,
        payload: &impl Serialize,
    ) -> Result<AuthResponse, ApiError> {
        let response = self
            .client
            .post(self.url(path)?)
            .json(payload)
            .send()
            .await?;
        parse_json(response).await
    }

    pub async fn me(&self, token: &str) -> Result<AuthUser, ApiError> {
        let response = self
            .client
            .get(self.url("/v1/auth/me")?)
            .bearer_auth(token)
            .send()
            .await?;
        Ok(parse_json::<MeResponse>(response).await?.user)
    }

    pub async fn logout(&self, token: &str) -> Result<(), ApiError> {
        let response = self
            .client
            .post(self.url("/v1/auth/logout")?)
            .bearer_auth(token)
            .send()
            .await?;
        ensure_success(response).await.map(|_| ())
    }

    pub async fn create_session(&self, token: &str) -> Result<SessionEnvelope, ApiError> {
        let response = self
            .client
            .post(self.url("/v1/sessions")?)
            .bearer_auth(token)
            .json(&serde_json::json!({"auto_opener": crate::product::AUTO_OPENER}))
            .send()
            .await?;
        parse_json(response).await
    }

    pub async fn latest_session(&self, token: &str) -> Result<SessionEnvelope, ApiError> {
        let response = self
            .client
            .get(self.url("/v1/sessions/latest")?)
            .bearer_auth(token)
            .send()
            .await?;
        parse_json(response).await
    }

    pub async fn get_session(
        &self,
        token: &str,
        session_id: &str,
    ) -> Result<SessionState, ApiError> {
        let response = self
            .client
            .get(self.url(&format!("/v1/sessions/{session_id}"))?)
            .bearer_auth(token)
            .send()
            .await?;
        parse_json(response).await
    }

    pub async fn post_event(
        &self,
        token: &str,
        session_id: &str,
        event: &SessionEventRequest,
    ) -> Result<(), ApiError> {
        let response = self
            .client
            .post(self.url(&format!("/v1/sessions/{session_id}/events"))?)
            .bearer_auth(token)
            .json(event)
            .send()
            .await?;
        ensure_success(response).await.map(|_| ())
    }

    pub async fn session_stream(
        &self,
        token: &str,
        session_id: &str,
    ) -> Result<Response, ApiError> {
        let response = self
            .client
            .get(self.url(&format!("/v1/sessions/{session_id}/stream"))?)
            .bearer_auth(token)
            .header("Accept", "text/event-stream")
            .send()
            .await?;
        ensure_success(response).await
    }

    fn url(&self, path: &str) -> Result<Url, ApiError> {
        self.base_url.join(path).map_err(|error| ApiError {
            status: None,
            message: format!("invalid backend path {path}: {error}"),
        })
    }
}

async fn ensure_success(response: Response) -> Result<Response, ApiError> {
    if response.status().is_success() {
        return Ok(response);
    }
    let status = response.status();
    let path = response.url().path().to_string();
    let fallback = format!("HTTP {status}");
    let message = response
        .json::<serde_json::Value>()
        .await
        .ok()
        .and_then(|value| {
            value
                .get("error")
                .and_then(|value| value.as_str())
                .map(str::to_owned)
        })
        .unwrap_or(fallback);
    tracing::warn!(%status, %path, error = %message, "backend request failed");
    Err(ApiError {
        status: Some(status),
        message,
    })
}

async fn parse_json<T: serde::de::DeserializeOwned>(response: Response) -> Result<T, ApiError> {
    let response = ensure_success(response).await?;
    let status = response.status();
    let path = response.url().path().to_string();
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("unknown")
        .to_string();
    let body = response.bytes().await?;
    serde_json::from_slice(&body).map_err(|error| {
        tracing::error!(
            %status,
            %path,
            %content_type,
            body_bytes = body.len(),
            decode_error = %error,
            "backend JSON decode failed"
        );
        ApiError {
            status: Some(status),
            message: format!("decode response body for {path}: {error}"),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    #[test]
    fn production_http_url_becomes_secure_websocket_url() {
        let api = ApiClient::production().unwrap();
        let url = api.websocket_url("/v1/sessions/sess-1/stt/live").unwrap();
        assert_eq!(
            url.as_str(),
            "wss://rec.teamgenius.ru/v1/sessions/sess-1/stt/live"
        );
    }

    async fn mock_response(status: &str, body: &str) -> (String, tokio::task::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let status = status.to_string();
        let body = body.to_string();
        let task = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0_u8; 8192];
            let size = socket.read(&mut request).await.unwrap();
            let response = format!(
                "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            String::from_utf8_lossy(&request[..size]).into_owned()
        });
        (format!("http://{address}"), task)
    }

    #[tokio::test]
    async fn login_uses_gateway_contract_against_local_mock() {
        let body = r#"{"user":{"id":"u1","email":"sales@example.com","role":"sales"},"token":"secret","expires_at":"2030-01-01T00:00:00Z"}"#;
        let (base_url, request) = mock_response("200 OK", body).await;
        let auth = ApiClient::new(&base_url)
            .unwrap()
            .login("sales@example.com", "password-123")
            .await
            .unwrap();

        assert_eq!(auth.user.role, "sales");
        assert_eq!(auth.token, "secret");
        let request = request.await.unwrap();
        assert!(request.starts_with("POST /v1/auth/login HTTP/1.1"));
        assert!(request.contains("sales@example.com"));
    }

    #[tokio::test]
    async fn unauthorized_response_is_typed_for_auth_cleanup() {
        let (base_url, request) =
            mock_response("401 Unauthorized", r#"{"error":"expired token"}"#).await;
        let error = ApiClient::new(&base_url)
            .unwrap()
            .me("expired")
            .await
            .unwrap_err();

        assert!(error.unauthorized());
        assert_eq!(error.message, "expired token");
        let request = request.await.unwrap().to_ascii_lowercase();
        assert!(request.contains("authorization: bearer expired"));
    }

    #[tokio::test]
    async fn latest_session_accepts_production_nullable_collections() {
        let body = r#"{
          "session_id":"sess-production",
          "state":{
            "session_id":"sess-production",
            "messages":null,
            "transcript":null,
            "assist":{},
            "events":[]
          }
        }"#;
        let (base_url, request) = mock_response("200 OK", body).await;
        let envelope = ApiClient::new(&base_url)
            .unwrap()
            .latest_session("token")
            .await
            .unwrap();

        assert_eq!(envelope.session_id, "sess-production");
        assert!(envelope.state.messages.is_empty());
        assert!(envelope.state.transcript.is_empty());
        assert!(request
            .await
            .unwrap()
            .starts_with("GET /v1/sessions/latest HTTP/1.1"));
    }
}
