package clean

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	EventSessionCreated  = "session.created"
	EventSellerInput     = "seller.input"
	EventClientPartial   = "client.partial"
	EventClientFinal     = "client.final"
	EventSellerRequest   = "seller.request"
	EventSellerStarted   = "seller.started"
	EventSellerDelta     = "seller.delta"
	EventSellerDone      = "seller.done"
	EventSellerCanceled  = "seller.canceled"
	EventAssistRequest   = "assist.request"
	EventAssistStarted   = "assist.started"
	EventAssistFastDone  = "assist.fast.done"
	EventAssistDelta     = "assist.delta"
	EventAssistDone      = "assist.done"
	EventAssistCanceled  = "assist.canceled"
	EventSTTPartial      = "stt.partial"
	EventSTTFinal        = "stt.final"
	EventStageRequest    = "stage.request"
	EventStageCandidate  = "stage.candidate"
	EventStageCommitted  = "stage.committed"
	EventScorecardUpdate = "scorecard.update"
	EventError           = "error"
)

type Event struct {
	ID           string          `json:"id"`
	SessionID    string          `json:"session_id"`
	Type         string          `json:"type"`
	Source       string          `json:"source"`
	GenerationID string          `json:"generation_id,omitempty"`
	CreatedAt    time.Time       `json:"created_at"`
	Data         json.RawMessage `json:"data,omitempty"`
}

type TextData struct {
	Text string `json:"text"`
}

type SpeechData struct {
	Role   string `json:"role"`
	Text   string `json:"text"`
	Source string `json:"source,omitempty"`
}

type SellerRequestData struct {
	Trigger string `json:"trigger"`
	Text    string `json:"text,omitempty"`
}

type AssistRequestData struct {
	Trigger string `json:"trigger"`
	Text    string `json:"text,omitempty"`
}

type SellerStartedData struct {
	GenerationID string `json:"generation_id"`
	Trigger      string `json:"trigger"`
}

type AssistStartedData struct {
	GenerationID string `json:"generation_id"`
	Trigger      string `json:"trigger"`
}

type SellerDeltaData struct {
	GenerationID string `json:"generation_id"`
	Delta        string `json:"delta"`
}

type AssistFastDoneData struct {
	GenerationID string `json:"generation_id"`
	Text         string `json:"text"`
	Model        string `json:"model,omitempty"`
	Fallback     bool   `json:"fallback,omitempty"`
}

type AssistDeltaData struct {
	GenerationID string `json:"generation_id"`
	Delta        string `json:"delta"`
}

type SellerDoneData struct {
	GenerationID string `json:"generation_id"`
	Text         string `json:"text"`
	Provider     string `json:"provider"`
	Model        string `json:"model"`
}

type AssistDoneData struct {
	GenerationID string `json:"generation_id"`
	FastText     string `json:"fast_text"`
	SlowText     string `json:"slow_text"`
	FastModel    string `json:"fast_model,omitempty"`
	SlowModel    string `json:"slow_model,omitempty"`
}

type StageData struct {
	Stage      string          `json:"stage"`
	Title      string          `json:"title,omitempty"`
	Agenda     string          `json:"agenda,omitempty"`
	Emotion    string          `json:"emotion,omitempty"`
	Step       string          `json:"step,omitempty"`
	Provider   string          `json:"provider,omitempty"`
	Model      string          `json:"model,omitempty"`
	Confidence *float64        `json:"confidence,omitempty"`
	Scorecard  json.RawMessage `json:"scorecard,omitempty"`
}

type ScorecardData struct {
	Readiness      string          `json:"readiness"`
	ReadinessLabel string          `json:"readiness_label"`
	ReadyToAdvance bool            `json:"ready_to_advance"`
	NextAction     string          `json:"next_action"`
	Summary        string          `json:"summary"`
	Source         string          `json:"source"`
	Raw            json.RawMessage `json:"raw,omitempty"`
}

type ErrorData struct {
	Message string `json:"message"`
	Where   string `json:"where,omitempty"`
}

func NewEvent(sessionID, typ, source string, data any) Event {
	raw, _ := json.Marshal(data)
	return Event{
		ID:        NewID("evt"),
		SessionID: sessionID,
		Type:      typ,
		Source:    source,
		CreatedAt: time.Now().UTC(),
		Data:      raw,
	}
}

func Subject(prefix, sessionID, typ string) string {
	return strings.Trim(prefix, ".") + "." + sessionID + "." + typ
}

func ParseSubject(prefix, subject string) (sessionID string, typ string, ok bool) {
	base := strings.Trim(prefix, ".") + "."
	if !strings.HasPrefix(subject, base) {
		return "", "", false
	}
	rest := strings.TrimPrefix(subject, base)
	parts := strings.SplitN(rest, ".", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

func DecodeData[T any](event Event) (T, error) {
	var out T
	if len(event.Data) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(event.Data, &out); err != nil {
		return out, fmt.Errorf("decode %s data: %w", event.Type, err)
	}
	return out, nil
}

func NewID(prefix string) string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
	}
	return prefix + "-" + hex.EncodeToString(b[:])
}
