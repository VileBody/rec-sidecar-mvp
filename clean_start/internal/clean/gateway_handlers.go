package clean

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

type CreateSessionRequest struct {
	AutoOpener bool `json:"auto_opener"`
}

type CreateSessionResponse struct {
	SessionID string       `json:"session_id"`
	State     SessionState `json:"state"`
}

type InputEventRequest struct {
	Type      string `json:"type"`
	Text      string `json:"text,omitempty"`
	Trigger   string `json:"trigger,omitempty"`
	Role      string `json:"role,omitempty"`
	Source    string `json:"source,omitempty"`
	Speaker   string `json:"speaker,omitempty"`
	SegmentID string `json:"segment_id,omitempty"`
	Direction string `json:"direction,omitempty"`
}

type BrowserAudioLogRequest struct {
	Mode   string `json:"mode,omitempty"`
	Role   string `json:"role,omitempty"`
	Source string `json:"source,omitempty"`
	Event  string `json:"event"`
	Detail string `json:"detail,omitempty"`
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

func (g *Gateway) staticAsset(w http.ResponseWriter, r *http.Request) {
	asset := strings.TrimSpace(r.PathValue("asset"))
	var contentType string
	switch asset {
	case "styles.css":
		contentType = "text/css; charset=utf-8"
	case "app.js":
		contentType = "text/javascript; charset=utf-8"
	default:
		http.NotFound(w, r)
		return
	}
	raw, err := gatewayWeb.ReadFile("web/" + asset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "no-cache")
	_, _ = w.Write(raw)
}

func (g *Gateway) healthz(w http.ResponseWriter, _ *http.Request) {
	sttProvider, sttConfigured := g.sttStatus()
	natsURL := ""
	if g.nc != nil {
		natsURL = g.nc.ConnectedUrl()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                  true,
		"role":                g.cfg.Role,
		"nats_url":            natsURL,
		"auth_enabled":        g.cfg.AuthEnabled,
		"coach_enabled":       g.cfg.CoachEnabled,
		"stt_provider":        sttProvider,
		"stt_configured":      sttConfigured,
		"soniox_configured":   g.soniox != nil && g.soniox.Configured(),
		"inworld_configured":  g.inworld != nil && g.inworld.Configured(),
		"audio_s3_configured": g.audioSink != nil && g.audioSink.Configured(),
	})
}

func (g *Gateway) createSession(w http.ResponseWriter, r *http.Request) {
	user, ok := g.requireUser(w, r)
	if !ok {
		return
	}
	var req CreateSessionRequest
	req.AutoOpener = g.cfg.CoachEnabled
	if r.Body != nil {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && !errors.Is(err, io.EOF) {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	}
	sessionID := NewID("sess")
	if g.authRequired() {
		if err := g.authStore.CreateAppSession(r.Context(), sessionID, user.ID); err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
	}
	created := NewEvent(sessionID, EventSessionCreated, "gateway", map[string]any{})
	if err := g.emit(created); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if g.cfg.CoachEnabled && req.AutoOpener {
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
	if _, ok := g.requireSessionOwner(w, r, sessionID); !ok {
		return
	}
	state, ok, err := g.hydrateSession(r.Context(), sessionID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if !ok {
		writeError(w, http.StatusNotFound, fmt.Errorf("session %s not found", sessionID))
		return
	}
	writeJSON(w, http.StatusOK, state)
}

func (g *Gateway) latestSession(w http.ResponseWriter, r *http.Request) {
	user, ok := g.requireUser(w, r)
	if !ok {
		return
	}
	if !g.authRequired() {
		writeError(w, http.StatusNotFound, errors.New("latest session is available only when auth is enabled"))
		return
	}
	sessionID, err := g.authStore.LatestAppSession(r.Context(), user.ID)
	if err != nil {
		if errors.Is(err, ErrAuthNotFound) {
			writeError(w, http.StatusNotFound, errors.New("no previous session"))
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	state, ok, err := g.hydrateSession(r.Context(), sessionID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if !ok {
		writeError(w, http.StatusNotFound, fmt.Errorf("session %s not found", sessionID))
		return
	}
	writeJSON(w, http.StatusOK, CreateSessionResponse{SessionID: sessionID, State: state})
}

func (g *Gateway) postEvent(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if _, ok := g.requireSessionOwner(w, r, sessionID); !ok {
		return
	}
	if _, ok, err := g.hydrateSession(r.Context(), sessionID); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	} else if !ok {
		writeError(w, http.StatusNotFound, fmt.Errorf("session %s not found", sessionID))
		return
	}
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
		if !g.cfg.CoachEnabled {
			g.logger.Info("coach request ignored", "session_id", sessionID, "type", req.Type, "reason", "coach_disabled")
			state, _, _ := g.hydrateSession(r.Context(), sessionID)
			writeJSON(w, http.StatusAccepted, state)
			return
		}
		trigger := req.Trigger
		if trigger == "" {
			trigger = "manual"
		}
		event = NewEvent(sessionID, EventSellerRequest, "gateway", SellerRequestData{Trigger: trigger, Text: strings.TrimSpace(req.Text)})
	case EventAssistRequest:
		if !g.cfg.CoachEnabled {
			g.logger.Info("coach request ignored", "session_id", sessionID, "type", req.Type, "reason", "coach_disabled")
			state, _, _ := g.hydrateSession(r.Context(), sessionID)
			writeJSON(w, http.StatusAccepted, state)
			return
		}
		trigger := req.Trigger
		if trigger == "" {
			trigger = "manual"
		}
		event = NewEvent(sessionID, EventAssistRequest, "gateway", AssistRequestData{Trigger: trigger, Text: strings.TrimSpace(req.Text)})
	case EventStudentDirection:
		direction, err := normalizeStudentDirection(req.Direction)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		event = NewEvent(sessionID, EventStudentDirection, "gateway", StudentDirectionData{Direction: direction})
	case EventStudentInput:
		direction, err := normalizeStudentDirection(req.Direction)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		event = NewEvent(sessionID, EventStudentInput, "gateway", StudentInputData{Text: strings.TrimSpace(req.Text), Direction: direction})
	case EventStudentAnswerRequest:
		trigger := req.Trigger
		if trigger == "" {
			trigger = "manual"
		}
		event = NewEvent(sessionID, EventStudentAnswerRequest, "gateway", StudentAnswerRequestData{Trigger: trigger, Text: strings.TrimSpace(req.Text)})
	case EventSTTPartial, EventSTTFinal:
		role := strings.TrimSpace(req.Role)
		if role == "" {
			role = "client"
		}
		event = NewEvent(sessionID, req.Type, "gateway", SpeechData{
			Role:      role,
			Text:      strings.TrimSpace(req.Text),
			Source:    strings.TrimSpace(req.Source),
			Speaker:   strings.TrimSpace(req.Speaker),
			SegmentID: strings.TrimSpace(req.SegmentID),
			Direction: strings.TrimSpace(req.Direction),
		})
	default:
		writeError(w, http.StatusBadRequest, fmt.Errorf("unsupported event type %q", req.Type))
		return
	}

	if err := g.emit(event); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	state, _, _ := g.hydrateSession(r.Context(), sessionID)
	writeJSON(w, http.StatusAccepted, state)
}

func (g *Gateway) logBrowserAudio(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if _, ok := g.requireSessionOwner(w, r, sessionID); !ok {
		return
	}
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
