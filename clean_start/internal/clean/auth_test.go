package clean

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newAuthTestGateway() *Gateway {
	g := newHTTPTestGateway(Config{
		AuthEnabled:    true,
		JWTSecret:      "test-secret",
		JWTTTL:         24 * time.Hour,
		AuthCookieName: "clean_start_token",
	})
	g.authStore = NewMemoryAuthStore()
	g.tokens = NewTokenManager(g.cfg.JWTSecret)
	return g
}

func authJSON[T any](t *testing.T, rec *httptest.ResponseRecorder) T {
	t.Helper()
	var out T
	if err := json.NewDecoder(rec.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}

func registerTestUser(t *testing.T, g *Gateway, email string) AuthResponse {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/auth/register", strings.NewReader(`{"email":"`+email+`","password":"password123"}`))
	rec := httptest.NewRecorder()
	g.register(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("register status = %d body=%s", rec.Code, rec.Body.String())
	}
	return authJSON[AuthResponse](t, rec)
}

func TestAuthRegisterLoginMeAndLogout(t *testing.T) {
	g := newAuthTestGateway()
	registered := registerTestUser(t, g, "Seller@Example.com")
	if registered.User.Email != "seller@example.com" || registered.Token == "" {
		t.Fatalf("unexpected register response: %#v", registered)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/auth/me", nil)
	req.Header.Set("Authorization", "Bearer "+registered.Token)
	rec := httptest.NewRecorder()
	g.me(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("me status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer "+registered.Token)
	rec = httptest.NewRecorder()
	g.logout(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("logout status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/auth/me", nil)
	req.Header.Set("Authorization", "Bearer "+registered.Token)
	rec = httptest.NewRecorder()
	g.me(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("revoked token status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/auth/login", strings.NewReader(`{"email":"seller@example.com","password":"password123"}`))
	rec = httptest.NewRecorder()
	g.login(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("login status = %d body=%s", rec.Code, rec.Body.String())
	}
	loggedIn := authJSON[AuthResponse](t, rec)
	if loggedIn.Token == "" || loggedIn.User.ID != registered.User.ID {
		t.Fatalf("unexpected login response: %#v", loggedIn)
	}
}

func TestAuthRejectsDuplicateAndBadCredentials(t *testing.T) {
	g := newAuthTestGateway()
	registerTestUser(t, g, "seller@example.com")

	req := httptest.NewRequest(http.MethodPost, "/v1/auth/register", strings.NewReader(`{"email":"seller@example.com","password":"password123"}`))
	rec := httptest.NewRecorder()
	g.register(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("duplicate status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/auth/login", strings.NewReader(`{"email":"seller@example.com","password":"wrong"}`))
	rec = httptest.NewRecorder()
	g.login(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("bad login status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestAuthRegisterSupportsStudentRole(t *testing.T) {
	g := newAuthTestGateway()
	req := httptest.NewRequest(http.MethodPost, "/v1/auth/register", strings.NewReader(`{"email":"student@example.com","password":"password123","role":"student"}`))
	rec := httptest.NewRecorder()

	g.register(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("register status = %d body=%s", rec.Code, rec.Body.String())
	}
	registered := authJSON[AuthResponse](t, rec)
	if registered.User.Role != UserRoleStudent {
		t.Fatalf("role = %q, want student", registered.User.Role)
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/auth/register", strings.NewReader(`{"email":"bad-role@example.com","password":"password123","role":"admin"}`))
	rec = httptest.NewRecorder()
	g.register(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("bad role status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestAuthRecognizesAdminRole(t *testing.T) {
	role, err := normalizeUserRole(" Admin ")
	if err != nil {
		t.Fatal(err)
	}
	if role != UserRoleAdmin {
		t.Fatalf("role = %q, want admin", role)
	}
	user := authUserResponse(User{ID: "usr-admin", Email: "admin@example.com", Role: role})
	if user.Role != UserRoleAdmin {
		t.Fatalf("response role = %q, want admin", user.Role)
	}
}

func TestSessionOwnershipIsolation(t *testing.T) {
	g := newAuthTestGateway()
	userA := registerTestUser(t, g, "a@example.com")
	userB := registerTestUser(t, g, "b@example.com")

	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":false}`))
	req.Header.Set("Authorization", "Bearer "+userA.Token)
	rec := httptest.NewRecorder()
	g.createSession(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create session status = %d body=%s", rec.Code, rec.Body.String())
	}
	session := authJSON[CreateSessionResponse](t, rec)

	req = httptest.NewRequest(http.MethodGet, "/v1/sessions/"+session.SessionID, nil)
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+userB.Token)
	rec = httptest.NewRecorder()
	g.getSession(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-user get status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/sessions/"+session.SessionID+"/events", strings.NewReader(`{"type":"seller.input","text":"hi"}`))
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+userB.Token)
	rec = httptest.NewRecorder()
	g.postEvent(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-user post status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/sessions/"+session.SessionID, nil)
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+userA.Token)
	rec = httptest.NewRecorder()
	g.getSession(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("owner get status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestAuthRequiredForSessionCreation(t *testing.T) {
	g := newAuthTestGateway()
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":false}`))
	rec := httptest.NewRecorder()
	g.createSession(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauth create status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestLatestSessionHydratesPersistedEvents(t *testing.T) {
	g := newAuthTestGateway()
	user := registerTestUser(t, g, "persist@example.com")

	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":false}`))
	req.Header.Set("Authorization", "Bearer "+user.Token)
	rec := httptest.NewRecorder()
	g.createSession(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create session status = %d body=%s", rec.Code, rec.Body.String())
	}
	session := authJSON[CreateSessionResponse](t, rec)

	req = httptest.NewRequest(http.MethodPost, "/v1/sessions/"+session.SessionID+"/events", strings.NewReader(`{"type":"seller.input","text":"Здравствуйте"}`))
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+user.Token)
	rec = httptest.NewRecorder()
	g.postEvent(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("post event status = %d body=%s", rec.Code, rec.Body.String())
	}

	g.store = NewStore()
	req = httptest.NewRequest(http.MethodGet, "/v1/sessions/latest", nil)
	req.Header.Set("Authorization", "Bearer "+user.Token)
	rec = httptest.NewRecorder()
	g.latestSession(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("latest session status = %d body=%s", rec.Code, rec.Body.String())
	}
	latest := authJSON[CreateSessionResponse](t, rec)
	if latest.SessionID != session.SessionID {
		t.Fatalf("latest session id = %q, want %q", latest.SessionID, session.SessionID)
	}
	if len(latest.State.Messages) != 1 || latest.State.Messages[0].Text != "Здравствуйте" {
		t.Fatalf("hydrated messages = %#v", latest.State.Messages)
	}
}
