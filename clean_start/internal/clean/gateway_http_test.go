package clean

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
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

func TestGatewayClientTelemetryLogsStructuredDiagnostics(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	sessionID := "sess-client-telemetry"
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))

	var logs bytes.Buffer
	g.logger = slog.New(slog.NewJSONHandler(&logs, nil))
	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/sessions/"+sessionID+"/telemetry/client-log",
		strings.NewReader(`{
			"event":"system_capture_failed",
			"source":"remote_audio",
			"detail":"audio_track_missing",
			"data":{"reason_code":"audio_track_missing","audio_track_count":0}
		}`),
	)
	req.SetPathValue("session_id", sessionID)
	rec := httptest.NewRecorder()

	g.logClientTelemetry(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	logged := logs.String()
	if !strings.Contains(logged, `"reason_code":"audio_track_missing"`) {
		t.Fatalf("structured diagnostic missing from log: %s", logged)
	}
	if !strings.Contains(logged, `"audio_track_count":0`) {
		t.Fatalf("track count missing from log: %s", logged)
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
	if item.Role != "client" || item.RoleReason != "source:remote_audio" || item.Text != "Привет" || item.Source != CaptureSourceRemoteAudio || item.Speaker != "1" || item.SegmentID != "seg" {
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
	indexRaw, err := gatewayWeb.ReadFile("web/index.html")
	if err != nil {
		t.Fatal(err)
	}
	appRaw, err := gatewayWeb.ReadFile("web/app.js")
	if err != nil {
		t.Fatal(err)
	}
	html := string(indexRaw) + "\n" + string(appRaw)
	for _, want := range []string{
		`/static/styles.css`,
		`/static/app.js`,
		`id="authPanel"`,
		`/v1/auth/me`,
		`/v1/auth/login`,
		`/v1/auth/register`,
		`/v1/sessions/latest`,
		`id="logout"`,
		`id="studentApp"`,
		`id="studentLogout"`,
		`id="studentDirection"`,
		`student.answer.request`,
		`student.input`,
		`id="personalApp"`,
		`id="personalTranscript"`,
		`id="personalLogout"`,
		`function isPersonalUser()`,
		`function waitForPersonalSession()`,
		`веб-режим · только просмотр`,
	} {
		if !strings.Contains(html, want) {
			t.Fatalf("index.html missing %q", want)
		}
	}
}

func TestGatewayStaticAssets(t *testing.T) {
	g := newHTTPTestGateway(Config{})
	for _, tc := range []struct {
		path        string
		asset       string
		contentType string
		contains    string
	}{
		{path: "/static/styles.css", asset: "styles.css", contentType: "text/css; charset=utf-8", contains: ".app"},
		{path: "/static/app.js", asset: "app.js", contentType: "text/javascript; charset=utf-8", contains: "function render"},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.SetPathValue("asset", tc.asset)
		rec := httptest.NewRecorder()

		g.staticAsset(rec, req)

		if rec.Code != http.StatusOK {
			t.Fatalf("%s status = %d body=%s", tc.path, rec.Code, rec.Body.String())
		}
		if got := rec.Header().Get("Content-Type"); got != tc.contentType {
			t.Fatalf("%s content-type = %q", tc.path, got)
		}
		if !strings.Contains(rec.Body.String(), tc.contains) {
			t.Fatalf("%s missing %q", tc.path, tc.contains)
		}
	}

	req := httptest.NewRequest(http.MethodGet, "/static/nope.txt", nil)
	req.SetPathValue("asset", "nope.txt")
	rec := httptest.NewRecorder()
	g.staticAsset(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown asset status = %d body=%s", rec.Code, rec.Body.String())
	}
}
