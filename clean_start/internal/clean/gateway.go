package clean

import (
	"context"
	"embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

//go:embed web/index.html
var gatewayWeb embed.FS

type Gateway struct {
	cfg     Config
	nc      *nats.Conn
	inworld *InworldClient
	logger  *slog.Logger
	store   *Store
	server  *http.Server
	sub     *nats.Subscription
}

type CreateSessionRequest struct {
	AutoOpener bool `json:"auto_opener"`
}

type CreateSessionResponse struct {
	SessionID string       `json:"session_id"`
	State     SessionState `json:"state"`
}

type InputEventRequest struct {
	Type    string `json:"type"`
	Text    string `json:"text,omitempty"`
	Trigger string `json:"trigger,omitempty"`
	Role    string `json:"role,omitempty"`
	Source  string `json:"source,omitempty"`
}

type STTTranscribeRequest struct {
	Role      string `json:"role,omitempty"`
	Source    string `json:"source,omitempty"`
	PCMBase64 string `json:"pcm_base64"`
}

type STTTranscribeResponse struct {
	Text string `json:"text"`
	Role string `json:"role"`
}

func NewGateway(cfg Config, nc *nats.Conn, inworld *InworldClient, logger *slog.Logger) *Gateway {
	return &Gateway{
		cfg:     cfg,
		nc:      nc,
		inworld: inworld,
		logger:  logger.With("component", "gateway"),
		store:   NewStore(),
	}
}

func (g *Gateway) Run(ctx context.Context) error {
	subject := g.cfg.SubjectPrefix + ".*.>"
	sub, err := g.nc.Subscribe(subject, func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			g.logger.Warn("bad event payload", "subject", msg.Subject, "error", err)
			return
		}
		if event.SessionID == "" {
			if sessionID, _, ok := ParseSubject(g.cfg.SubjectPrefix, msg.Subject); ok {
				event.SessionID = sessionID
			}
		}
		g.store.Apply(event)
	})
	if err != nil {
		return err
	}
	g.sub = sub

	mux := http.NewServeMux()
	mux.HandleFunc("GET /", g.index)
	mux.HandleFunc("GET /healthz", g.healthz)
	mux.HandleFunc("POST /v1/sessions", g.createSession)
	mux.HandleFunc("GET /v1/sessions/{session_id}", g.getSession)
	mux.HandleFunc("POST /v1/sessions/{session_id}/events", g.postEvent)
	mux.HandleFunc("POST /v1/sessions/{session_id}/stt/transcribe", g.transcribePCM)
	mux.HandleFunc("GET /v1/sessions/{session_id}/stream", g.streamSession)

	g.server = &http.Server{
		Addr:              g.cfg.HTTPAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		_ = g.Shutdown(shutdownCtx)
	}()

	g.logger.Info("gateway listening", "addr", g.cfg.HTTPAddr)
	err = g.server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (g *Gateway) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	raw, err := gatewayWeb.ReadFile("web/index.html")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(raw)
}

func (g *Gateway) Shutdown(ctx context.Context) error {
	if g.sub != nil {
		_ = g.sub.Unsubscribe()
	}
	if g.server != nil {
		return g.server.Shutdown(ctx)
	}
	return nil
}

func (g *Gateway) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                 true,
		"role":               g.cfg.Role,
		"nats_url":           g.nc.ConnectedUrl(),
		"inworld_configured": g.inworld != nil && g.inworld.Configured(),
	})
}

func (g *Gateway) createSession(w http.ResponseWriter, r *http.Request) {
	var req CreateSessionRequest
	req.AutoOpener = true
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	sessionID := NewID("sess")
	created := NewEvent(sessionID, EventSessionCreated, "gateway", map[string]any{})
	if err := g.emit(created); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if req.AutoOpener {
		opener := NewEvent(sessionID, EventSellerRequest, "gateway", SellerRequestData{Trigger: "opener"})
		if err := g.emit(opener); err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
	}
	state, _ := g.store.Get(sessionID)
	writeJSON(w, http.StatusCreated, CreateSessionResponse{SessionID: sessionID, State: state})
}

func (g *Gateway) getSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	state, ok := g.store.Get(sessionID)
	if !ok {
		writeError(w, http.StatusNotFound, fmt.Errorf("session %s not found", sessionID))
		return
	}
	writeJSON(w, http.StatusOK, state)
}

func (g *Gateway) postEvent(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	var req InputEventRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}

	var event Event
	switch req.Type {
	case EventSellerInput:
		event = NewEvent(sessionID, EventSellerInput, "gateway", TextData{Text: strings.TrimSpace(req.Text)})
	case EventClientPartial:
		event = NewEvent(sessionID, EventClientPartial, "gateway", TextData{Text: strings.TrimSpace(req.Text)})
	case EventClientFinal:
		event = NewEvent(sessionID, EventClientFinal, "gateway", TextData{Text: strings.TrimSpace(req.Text)})
	case EventSellerRequest:
		trigger := req.Trigger
		if trigger == "" {
			trigger = "manual"
		}
		event = NewEvent(sessionID, EventSellerRequest, "gateway", SellerRequestData{Trigger: trigger, Text: strings.TrimSpace(req.Text)})
	case EventAssistRequest:
		trigger := req.Trigger
		if trigger == "" {
			trigger = "manual"
		}
		event = NewEvent(sessionID, EventAssistRequest, "gateway", AssistRequestData{Trigger: trigger, Text: strings.TrimSpace(req.Text)})
	case EventSTTPartial, EventSTTFinal:
		role := strings.TrimSpace(req.Role)
		if role == "" {
			role = "client"
		}
		event = NewEvent(sessionID, req.Type, "gateway", SpeechData{
			Role:   role,
			Text:   strings.TrimSpace(req.Text),
			Source: strings.TrimSpace(req.Source),
		})
	default:
		writeError(w, http.StatusBadRequest, fmt.Errorf("unsupported event type %q", req.Type))
		return
	}

	if err := g.emit(event); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	state, _ := g.store.Get(sessionID)
	writeJSON(w, http.StatusAccepted, state)
}

func (g *Gateway) transcribePCM(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if g.inworld == nil || !g.inworld.Configured() {
		writeError(w, http.StatusServiceUnavailable, errors.New("missing INWORLD_API_KEY"))
		return
	}
	var req STTTranscribeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(req.PCMBase64))
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("bad pcm_base64: %w", err))
		return
	}
	role := strings.TrimSpace(req.Role)
	if role == "" {
		role = "client"
	}
	text, err := g.inworld.TranscribePCM(r.Context(), raw)
	if err != nil {
		if errors.Is(err, ErrNoSpeech) {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		g.logger.Warn("system audio stt failed", "bytes", len(raw), "error", err)
		writeError(w, http.StatusBadGateway, err)
		return
	}
	event := NewEvent(sessionID, EventSTTFinal, "gateway-stt", SpeechData{
		Role:   role,
		Text:   text,
		Source: strings.TrimSpace(req.Source),
	})
	if err := g.emit(event); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, STTTranscribeResponse{Text: text, Role: role})
}

func (g *Gateway) streamSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, errors.New("streaming unsupported"))
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	events, cancel := g.store.Subscribe(sessionID)
	defer cancel()

	if state, ok := g.store.Get(sessionID); ok {
		writeSSE(w, "snapshot", state)
		flusher.Flush()
	}

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
		case event := <-events:
			writeSSE(w, "event", event)
			if state, ok := g.store.Get(sessionID); ok {
				writeSSE(w, "snapshot", state)
			}
			flusher.Flush()
		}
	}
}

func (g *Gateway) emit(event Event) error {
	g.store.Apply(event)
	return PublishEvent(g.nc, g.cfg, event)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]any{"error": err.Error()})
}

func writeSSE(w http.ResponseWriter, event string, value any) {
	raw, _ := json.Marshal(value)
	fmt.Fprintf(w, "event: %s\n", event)
	fmt.Fprintf(w, "data: %s\n\n", raw)
}
