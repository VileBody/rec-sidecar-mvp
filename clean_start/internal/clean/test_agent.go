package clean

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type TestAgentGateway struct {
	cfg     Config
	llm     *LLMClient
	inworld *InworldClient
	logger  *slog.Logger
	server  *http.Server
	mu      sync.Mutex
	history map[string][]Message
}

type CreateTestAgentSessionResponse struct {
	SessionID string `json:"session_id"`
}

type TestAgentTurnRequest struct {
	SessionID   string `json:"session_id,omitempty"`
	SellerText  string `json:"seller_text"`
	PersonaMode string `json:"persona_mode,omitempty"`
}

type TestAgentTurnResponse struct {
	SessionID         string         `json:"session_id"`
	SellerText        string         `json:"seller_text"`
	SellerTranscript  string         `json:"seller_transcript"`
	SellerAudioBase64 string         `json:"seller_audio_base64"`
	SellerAudioMIME   string         `json:"seller_audio_mime"`
	ClientText        string         `json:"client_text"`
	ClientAudioBase64 string         `json:"client_audio_base64"`
	ClientAudioMIME   string         `json:"client_audio_mime"`
	ClientProvider    string         `json:"client_provider"`
	ClientModel       string         `json:"client_model"`
	STTError          string         `json:"stt_error,omitempty"`
	TimingsMS         map[string]int `json:"timings_ms"`
}

type testAgentWSMessage struct {
	Type        string `json:"type"`
	SessionID   string `json:"session_id,omitempty"`
	SellerText  string `json:"seller_text,omitempty"`
	PersonaMode string `json:"persona_mode,omitempty"`
}

func NewTestAgentGateway(cfg Config, llm *LLMClient, inworld *InworldClient, logger *slog.Logger) *TestAgentGateway {
	return &TestAgentGateway{
		cfg:     cfg,
		llm:     llm,
		inworld: inworld,
		logger:  logger.With("component", "test-agent-gateway"),
		history: make(map[string][]Message),
	}
}

func (g *TestAgentGateway) Run(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", g.healthz)
	mux.HandleFunc("POST /v1/test-agent/sessions", g.createSession)
	mux.HandleFunc("POST /v1/test-agent/sessions/{session_id}/turn", g.postTurn)
	mux.HandleFunc("GET /v1/test-agent/ws", g.ws)

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

	g.logger.Info("test agent listening", "addr", g.cfg.HTTPAddr)
	err := g.server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (g *TestAgentGateway) Shutdown(ctx context.Context) error {
	if g.server != nil {
		return g.server.Shutdown(ctx)
	}
	return nil
}

func (g *TestAgentGateway) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                 true,
		"role":               "test-agent",
		"llm_service_url":    g.cfg.LLMServiceURL,
		"inworld_configured": g.inworld.Configured(),
	})
}

func (g *TestAgentGateway) createSession(w http.ResponseWriter, _ *http.Request) {
	sessionID := NewID("test")
	g.mu.Lock()
	g.history[sessionID] = nil
	g.mu.Unlock()
	writeJSON(w, http.StatusCreated, CreateTestAgentSessionResponse{SessionID: sessionID})
}

func (g *TestAgentGateway) postTurn(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	var req TestAgentTurnRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	req.SessionID = sessionID
	resp, err := g.runTurn(r.Context(), req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (g *TestAgentGateway) ws(w http.ResponseWriter, r *http.Request) {
	upgrader := websocket.Upgrader{
		CheckOrigin: func(*http.Request) bool { return true },
	}
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer conn.Close()

	sessionID := NewID("test")
	g.mu.Lock()
	g.history[sessionID] = nil
	g.mu.Unlock()
	_ = conn.WriteJSON(map[string]any{"type": "session.created", "session_id": sessionID})

	for {
		var msg testAgentWSMessage
		if err := conn.ReadJSON(&msg); err != nil {
			return
		}
		switch msg.Type {
		case "reset":
			sessionID = NewID("test")
			g.mu.Lock()
			g.history[sessionID] = nil
			g.mu.Unlock()
			_ = conn.WriteJSON(map[string]any{"type": "session.created", "session_id": sessionID})
		case "seller_text", "turn":
			if msg.SessionID != "" {
				sessionID = msg.SessionID
			}
			_ = conn.WriteJSON(map[string]any{"type": "turn.started", "session_id": sessionID})
			resp, err := g.runTurn(r.Context(), TestAgentTurnRequest{
				SessionID:   sessionID,
				SellerText:  msg.SellerText,
				PersonaMode: msg.PersonaMode,
			})
			if err != nil {
				_ = conn.WriteJSON(map[string]any{"type": "error", "error": err.Error(), "session_id": sessionID})
				continue
			}
			_ = conn.WriteJSON(map[string]any{"type": "turn.result", "data": resp})
		default:
			_ = conn.WriteJSON(map[string]any{"type": "error", "error": "unknown message type", "session_id": sessionID})
		}
	}
}

func (g *TestAgentGateway) runTurn(ctx context.Context, req TestAgentTurnRequest) (TestAgentTurnResponse, error) {
	sessionID := strings.TrimSpace(req.SessionID)
	if sessionID == "" {
		sessionID = NewID("test")
	}
	sellerText := strings.TrimSpace(req.SellerText)
	if sellerText == "" {
		return TestAgentTurnResponse{}, errors.New("seller_text is required")
	}
	timings := make(map[string]int)

	start := time.Now()
	sellerAudio, err := g.inworld.Synthesize(ctx, "seller", sellerText)
	if err != nil {
		return TestAgentTurnResponse{}, fmt.Errorf("seller tts: %w", err)
	}
	timings["seller_tts"] = int(time.Since(start).Milliseconds())

	start = time.Now()
	sellerTranscript, sttErr := g.inworld.TranscribePCM(ctx, sellerAudio.PCM)
	timings["seller_stt"] = int(time.Since(start).Milliseconds())
	sttErrorText := ""
	if sttErr != nil {
		sttErrorText = sttErr.Error()
		sellerTranscript = sellerText
	}

	g.mu.Lock()
	history := append([]Message(nil), g.history[sessionID]...)
	g.mu.Unlock()

	start = time.Now()
	clientText, provider, model, err := g.llm.GenerateClientReply(ctx, sessionID, testAgentContext(history, req.PersonaMode), sellerTranscript)
	if err != nil {
		return TestAgentTurnResponse{}, fmt.Errorf("client actor: %w", err)
	}
	clientText = strings.TrimSpace(clientText)
	if clientText == "" {
		return TestAgentTurnResponse{}, errors.New("client actor returned empty reply")
	}
	timings["client_llm"] = int(time.Since(start).Milliseconds())

	start = time.Now()
	clientAudio, err := g.inworld.Synthesize(ctx, "client", clientText)
	if err != nil {
		return TestAgentTurnResponse{}, fmt.Errorf("client tts: %w", err)
	}
	timings["client_tts"] = int(time.Since(start).Milliseconds())

	now := time.Now().UTC()
	g.mu.Lock()
	g.history[sessionID] = append(g.history[sessionID],
		Message{Role: "seller", Text: sellerTranscript, CreatedAt: now},
		Message{Role: "client", Text: clientText, CreatedAt: now},
	)
	if len(g.history[sessionID]) > 40 {
		g.history[sessionID] = g.history[sessionID][len(g.history[sessionID])-40:]
	}
	g.mu.Unlock()

	return TestAgentTurnResponse{
		SessionID:         sessionID,
		SellerText:        sellerText,
		SellerTranscript:  sellerTranscript,
		SellerAudioBase64: base64.StdEncoding.EncodeToString(sellerAudio.WAV),
		SellerAudioMIME:   sellerAudio.MIME,
		ClientText:        clientText,
		ClientAudioBase64: base64.StdEncoding.EncodeToString(clientAudio.WAV),
		ClientAudioMIME:   clientAudio.MIME,
		ClientProvider:    provider,
		ClientModel:       model,
		STTError:          sttErrorText,
		TimingsMS:         timings,
	}, nil
}

func testAgentContext(history []Message, personaMode string) string {
	if personaMode == "" {
		personaMode = "hostile"
	}
	var b strings.Builder
	b.WriteString("Ты играешь клиента в тренировке продавца. Продукт: билеты на живой event Glubina Community в Казани для предпринимателей/экспертов, где продают нетворк, окружение, практику и новые возможности.\n")
	b.WriteString("Персона клиента: сложный, скептичный, не грубый ради грубости, но неприятный и требовательный. Режим: ")
	b.WriteString(personaMode)
	b.WriteString(". Не помогай продавцу явно, отвечай как настоящий покупатель.\n\n--- История ---\n")
	if len(history) == 0 {
		b.WriteString("(пока пусто)\n")
	}
	for _, msg := range history {
		role := "Client"
		if msg.Role == "seller" {
			role = "Seller"
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	return b.String()
}
