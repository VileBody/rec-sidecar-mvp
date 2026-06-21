package clean

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/nats-io/nats.go"
)

func newHTTPTestGateway(cfg Config) *Gateway {
	if cfg.SubjectPrefix == "" {
		cfg.SubjectPrefix = "clean.session"
	}
	g := &Gateway{
		cfg:    cfg,
		store:  NewStore(),
		logger: noopLogger(),
	}
	g.publish = func(_ *nats.Conn, _ Config, _ Event) error { return nil }
	return g
}

func TestGatewayCreateSession(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: true})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":false}`))
	rec := httptest.NewRecorder()

	g.createSession(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	var response CreateSessionResponse
	if err := json.NewDecoder(rec.Body).Decode(&response); err != nil {
		t.Fatal(err)
	}
	if response.SessionID == "" || response.State.SessionID != response.SessionID {
		t.Fatalf("unexpected response: %#v", response)
	}
	if len(response.State.Events) != 1 {
		t.Fatalf("auto_opener=false should only emit session.created, got %d events", len(response.State.Events))
	}
}

func TestGatewayCreateSessionRejectsMalformedJSON(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: true})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":`))
	rec := httptest.NewRecorder()

	g.createSession(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayGetSession(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	sessionID := "sess-get"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))

	req := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+sessionID, nil)
	req.SetPathValue("session_id", sessionID)
	rec := httptest.NewRecorder()
	g.getSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/sessions/missing", nil)
	req.SetPathValue("session_id", "missing")
	rec = httptest.NewRecorder()
	g.getSession(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayPostEventRequiresExistingSession(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: true})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/missing/events", strings.NewReader(`{"type":"seller.input","text":"hi"}`))
	req.SetPathValue("session_id", "missing")
	rec := httptest.NewRecorder()

	g.postEvent(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayPostEventAcceptsKnownEventsAndTrimsPayload(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: true})
	sessionID := "sess-post"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+sessionID+"/events", strings.NewReader(`{"type":"stt.final","text":"  Привет  ","role":"","source":" browser-system-audio ","speaker":" 1 ","segment_id":" seg "}`))
	req.SetPathValue("session_id", sessionID)
	rec := httptest.NewRecorder()

	g.postEvent(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		t.Fatal("session missing")
	}
	if len(state.Transcript) != 1 {
		t.Fatalf("transcript len = %d", len(state.Transcript))
	}
	item := state.Transcript[0]
	if item.Role != "client" || item.Text != "Привет" || item.Source != "browser-system-audio" || item.Speaker != "1" || item.SegmentID != "seg" {
		t.Fatalf("unexpected transcript item: %#v", item)
	}
}

func TestGatewayPostEventRejectsUnsupportedType(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: true})
	sessionID := "sess-unsupported"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+sessionID+"/events", strings.NewReader(`{"type":"wat"}`))
	req.SetPathValue("session_id", sessionID)
	rec := httptest.NewRecorder()

	g.postEvent(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayPostEventCoachDisabledReturnsCurrentState(t *testing.T) {
	g := newHTTPTestGateway(Config{CoachEnabled: false})
	sessionID := "sess-coach-disabled"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+sessionID+"/events", strings.NewReader(`{"type":"seller.request"}`))
	req.SetPathValue("session_id", sessionID)
	rec := httptest.NewRecorder()

	g.postEvent(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		t.Fatal("session missing")
	}
	if len(state.Events) != 1 {
		t.Fatalf("coach disabled should not publish request event, got %d events", len(state.Events))
	}
}

func TestGatewayStreamSessionRejectsUnknownSession(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	req := httptest.NewRequest(http.MethodGet, "/v1/sessions/missing/stream", nil)
	req.SetPathValue("session_id", "missing")
	rec := httptest.NewRecorder()

	g.streamSession(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayStreamSessionWritesInitialSnapshot(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	sessionID := "sess-stream"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	req := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+sessionID+"/stream", nil)
	req.SetPathValue("session_id", sessionID)
	ctx, cancel := context.WithCancel(req.Context())
	cancel()
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	g.streamSession(rec, req)

	body := rec.Body.String()
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, body)
	}
	if got := rec.Header().Get("Content-Type"); got != "text/event-stream" {
		t.Fatalf("content-type = %q", got)
	}
	if !strings.Contains(body, "event: snapshot") || !strings.Contains(body, `"session_id":"sess-stream"`) {
		t.Fatalf("stream body = %s", body)
	}
}

func TestGatewayEmitUsesPublishOverrideAndKeepsStoreUpdated(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	wantErr := errors.New("publish down")
	g.publish = func(_ *nats.Conn, _ Config, _ Event) error { return wantErr }
	event := NewEvent("sess-emit", EventSessionCreated, "test", map[string]any{})

	if err := g.emit(event); !errors.Is(err, wantErr) {
		t.Fatalf("emit err = %v, want %v", err, wantErr)
	}
	if _, ok := g.store.Get("sess-emit"); !ok {
		t.Fatal("emit should apply event before publish")
	}
}

func TestWriteJSONAndError(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusCreated, map[string]string{"ok": "yes"})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("content-type = %q", got)
	}
	if !strings.Contains(rec.Body.String(), `"ok":"yes"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}

	rec = httptest.NewRecorder()
	writeError(rec, http.StatusTeapot, errors.New("boom"))
	if rec.Code != http.StatusTeapot || !strings.Contains(rec.Body.String(), `"error":"boom"`) {
		t.Fatalf("unexpected error response: status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestWriteSSE(t *testing.T) {
	rec := httptest.NewRecorder()
	writeSSE(rec, "snapshot", map[string]string{"session_id": "sess"})
	if got := rec.Body.String(); got != "event: snapshot\ndata: {\"session_id\":\"sess\"}\n\n" {
		t.Fatalf("sse frame = %q", got)
	}
}

func TestGatewayWebIncludesAuthControls(t *testing.T) {
	raw, err := gatewayWeb.ReadFile("web/index.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(raw)
	for _, want := range []string{
		`id="authPanel"`,
		`/v1/auth/me`,
		`/v1/auth/login`,
		`/v1/auth/register`,
		`id="logout"`,
		`id="studentApp"`,
		`id="studentDirection"`,
		`student.answer.request`,
		`student.input`,
	} {
		if !strings.Contains(html, want) {
			t.Fatalf("index.html missing %q", want)
		}
	}
}
