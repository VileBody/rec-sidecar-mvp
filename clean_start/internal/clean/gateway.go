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
	"sync"
	"time"
	"unicode"

	"github.com/gorilla/websocket"
	"github.com/nats-io/nats.go"
)

//go:embed web/index.html
var gatewayWeb embed.FS

var sttWSUpgrader = websocket.Upgrader{
	ReadBufferSize:  64 * 1024,
	WriteBufferSize: 64 * 1024,
}

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

type BrowserAudioLogRequest struct {
	Mode   string `json:"mode,omitempty"`
	Role   string `json:"role,omitempty"`
	Source string `json:"source,omitempty"`
	Event  string `json:"event"`
	Detail string `json:"detail,omitempty"`
}

type BrowserSTTStreamMessage struct {
	AudioChunk  *AudioChunkMessage `json:"audio_chunk,omitempty"`
	EndTurn     map[string]any     `json:"end_turn,omitempty"`
	CloseStream map[string]any     `json:"close_stream,omitempty"`
}

type AudioChunkMessage struct {
	Content string `json:"content"`
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
	mux.HandleFunc("POST /v1/sessions/{session_id}/audio/log", g.logBrowserAudio)
	mux.HandleFunc("POST /v1/sessions/{session_id}/stt/transcribe", g.transcribePCM)
	mux.HandleFunc("GET /v1/sessions/{session_id}/stt/live", g.streamSTT)
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

func (g *Gateway) logBrowserAudio(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	var req BrowserAudioLogRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	event := strings.TrimSpace(req.Event)
	if event == "" {
		event = "unknown"
	}
	g.logger.Info(
		"browser audio client event",
		"session_id", sessionID,
		"event", event,
		"mode", strings.TrimSpace(req.Mode),
		"role", strings.TrimSpace(req.Role),
		"source", strings.TrimSpace(req.Source),
		"detail", strings.TrimSpace(req.Detail),
	)
	w.WriteHeader(http.StatusNoContent)
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
	source := strings.TrimSpace(req.Source)
	started := time.Now()
	g.logger.Info("browser audio stt received", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw))
	text, err := g.inworld.TranscribePCM(r.Context(), raw)
	elapsedMS := time.Since(started).Milliseconds()
	if err != nil {
		if errors.Is(err, ErrNoSpeech) {
			g.logger.Info("browser audio stt no speech", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw), "elapsed_ms", elapsedMS)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		g.logger.Warn("browser audio stt failed", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw), "elapsed_ms", elapsedMS, "error", err)
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if reason := browserTranscriptRejectReason(text); reason != "" {
		g.logger.Info("browser audio stt rejected", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw), "elapsed_ms", elapsedMS, "reason", reason, "text", text)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if reason := g.sellerEchoRejectReason(sessionID, role, source, text); reason != "" {
		g.logger.Info("browser audio stt rejected", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw), "elapsed_ms", elapsedMS, "reason", reason, "text", text)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	g.logger.Info("browser audio stt final", "session_id", sessionID, "role", role, "source", source, "bytes", len(raw), "elapsed_ms", elapsedMS, "text_len", len([]rune(text)), "text", text)
	event := NewEvent(sessionID, EventSTTFinal, "gateway-stt", SpeechData{
		Role:   role,
		Text:   text,
		Source: source,
	})
	if err := g.emit(event); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, STTTranscribeResponse{Text: text, Role: role})
}

func (g *Gateway) streamSTT(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if g.inworld == nil || !g.inworld.Configured() {
		writeError(w, http.StatusServiceUnavailable, errors.New("missing INWORLD_API_KEY"))
		return
	}
	role := strings.TrimSpace(r.URL.Query().Get("role"))
	if role == "" {
		role = "client"
	}
	source := strings.TrimSpace(r.URL.Query().Get("source"))
	if source == "" {
		source = "browser-audio"
	}

	browserConn, err := sttWSUpgrader.Upgrade(w, r, nil)
	if err != nil {
		g.logger.Warn("browser stt ws upgrade failed", "session_id", sessionID, "role", role, "source", source, "error", err)
		return
	}
	defer browserConn.Close()

	stream, err := g.inworld.ConnectSTT(r.Context())
	if err != nil {
		g.logger.Warn("browser stt inworld connect failed", "session_id", sessionID, "role", role, "source", source, "error", err)
		_ = browserConn.WriteJSON(map[string]any{"type": "error", "error": err.Error()})
		return
	}
	defer stream.Close()

	var browserWriteMu sync.Mutex
	writeBrowserJSON := func(value any) error {
		browserWriteMu.Lock()
		defer browserWriteMu.Unlock()
		return browserConn.WriteJSON(value)
	}

	_ = writeBrowserJSON(map[string]any{"type": "ready"})
	g.logger.Info("browser audio stt stream connected", "session_id", sessionID, "role", role, "source", source)

	done := make(chan error, 2)
	go func() {
		for {
			text, final, err := stream.ReadTranscript()
			if err != nil {
				done <- err
				return
			}
			if text == "" {
				continue
			}
			if reason := browserTranscriptRejectReason(text); reason != "" {
				g.logger.Info("browser audio stt stream rejected", "session_id", sessionID, "role", role, "source", source, "reason", reason, "text", text)
				continue
			}
			if reason := g.sellerEchoRejectReason(sessionID, role, source, text); reason != "" {
				g.logger.Info("browser audio stt stream rejected", "session_id", sessionID, "role", role, "source", source, "reason", reason, "text", text)
				continue
			}

			eventType := EventSTTPartial
			if final {
				eventType = EventSTTFinal
			}
			event := NewEvent(sessionID, eventType, "gateway-stt-live", SpeechData{
				Role:   role,
				Text:   text,
				Source: source,
			})
			if err := g.emit(event); err != nil {
				done <- err
				return
			}
			g.logger.Info("browser audio stt stream transcript", "session_id", sessionID, "role", role, "source", source, "final", final, "text_len", len([]rune(text)), "text", text)
			if err := writeBrowserJSON(map[string]any{"type": eventType, "text": text, "final": final}); err != nil {
				done <- err
				return
			}
		}
	}()

	go func() {
		var audioBytes int
		var audioChunks int
		var endTurns int
		for {
			var msg BrowserSTTStreamMessage
			if err := browserConn.ReadJSON(&msg); err != nil {
				g.logger.Info("browser audio stt stream reader closed", "session_id", sessionID, "role", role, "source", source, "audio_chunks", audioChunks, "audio_bytes", audioBytes, "end_turns", endTurns, "error", err)
				done <- err
				return
			}
			if msg.CloseStream != nil {
				g.logger.Info("browser audio stt stream close requested", "session_id", sessionID, "role", role, "source", source, "audio_chunks", audioChunks, "audio_bytes", audioBytes, "end_turns", endTurns)
				done <- nil
				return
			}
			if msg.EndTurn != nil {
				if err := stream.SendEndTurn(); err != nil {
					done <- fmt.Errorf("inworld stt stream end_turn: %w", err)
					return
				}
				endTurns++
				g.logger.Info("browser audio stt stream end_turn", "session_id", sessionID, "role", role, "source", source, "end_turns", endTurns)
				continue
			}
			if msg.AudioChunk == nil {
				continue
			}
			raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(msg.AudioChunk.Content))
			if err != nil {
				done <- fmt.Errorf("bad ws audio chunk: %w", err)
				return
			}
			if len(raw) == 0 {
				continue
			}
			if err := stream.SendAudio(raw); err != nil {
				done <- err
				return
			}
			audioChunks++
			audioBytes += len(raw)
		}
	}()

	err = <-done
	if err != nil && !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway, websocket.CloseNoStatusReceived) {
		g.logger.Warn("browser audio stt stream closed", "session_id", sessionID, "role", role, "source", source, "error", err)
		_ = writeBrowserJSON(map[string]any{"type": "error", "error": err.Error()})
		return
	}
	g.logger.Info("browser audio stt stream closed", "session_id", sessionID, "role", role, "source", source)
}

func browserTranscriptRejectReason(text string) string {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return "empty"
	}

	letters := 0
	cyrillic := 0
	nonRussianScript := 0
	for _, r := range trimmed {
		if !unicode.IsLetter(r) {
			continue
		}
		letters++
		switch {
		case unicode.In(r, unicode.Cyrillic):
			cyrillic++
		case isCJKLike(r):
			nonRussianScript++
		}
	}

	if letters <= 1 {
		return "too_short"
	}
	if cyrillic == 0 {
		if nonRussianScript > 0 {
			return "non_russian_script"
		}
		return "no_cyrillic"
	}
	if nonRussianScript > cyrillic {
		return "mostly_non_russian_script"
	}
	return ""
}

func isCJKLike(r rune) bool {
	return (r >= 0x3040 && r <= 0x30ff) || // Hiragana/Katakana
		(r >= 0x3400 && r <= 0x9fff) || // CJK ideographs
		(r >= 0xac00 && r <= 0xd7af) // Hangul
}

func (g *Gateway) sellerEchoRejectReason(sessionID, role, source, text string) string {
	if role != "client" || source != "browser-system-audio" {
		return ""
	}
	probe := normalizeEchoText(text)
	if len([]rune(probe)) < 8 {
		return ""
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		return ""
	}
	now := time.Now()
	for i := len(state.Messages) - 1; i >= 0; i-- {
		msg := state.Messages[i]
		if msg.Role != "seller" {
			continue
		}
		if now.Sub(msg.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(msg.Text)) >= 0.82 {
			return "seller_echo_message"
		}
	}
	for i := len(state.Transcript) - 1; i >= 0; i-- {
		item := state.Transcript[i]
		if item.Role != "seller" {
			continue
		}
		if now.Sub(item.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(item.Text)) >= 0.82 {
			return "seller_echo_transcript"
		}
	}
	return ""
}

func normalizeEchoText(text string) string {
	var b strings.Builder
	lastSpace := true
	for _, r := range strings.ToLower(text) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
			lastSpace = false
			continue
		}
		if !lastSpace {
			b.WriteRune(' ')
			lastSpace = true
		}
	}
	return strings.TrimSpace(b.String())
}

func textSimilarity(a, b string) float64 {
	if a == "" || b == "" {
		return 0
	}
	if strings.Contains(a, b) || strings.Contains(b, a) {
		shorter := len([]rune(a))
		longer := len([]rune(b))
		if shorter > longer {
			shorter, longer = longer, shorter
		}
		if longer == 0 {
			return 0
		}
		return float64(shorter) / float64(longer)
	}
	aTokens := tokenSet(a)
	bTokens := tokenSet(b)
	if len(aTokens) == 0 || len(bTokens) == 0 {
		return 0
	}
	intersections := 0
	for token := range aTokens {
		if _, ok := bTokens[token]; ok {
			intersections++
		}
	}
	return float64(2*intersections) / float64(len(aTokens)+len(bTokens))
}

func tokenSet(text string) map[string]struct{} {
	out := make(map[string]struct{})
	for _, token := range strings.Fields(text) {
		if len([]rune(token)) < 2 {
			continue
		}
		out[token] = struct{}{}
	}
	return out
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
