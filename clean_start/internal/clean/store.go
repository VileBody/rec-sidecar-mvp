package clean

import (
	"encoding/json"
	"sync"
	"time"
)

type Message struct {
	Role      string    `json:"role"`
	Text      string    `json:"text"`
	CreatedAt time.Time `json:"created_at"`
}

type TranscriptItem struct {
	ID        string    `json:"id,omitempty"`
	Role      string    `json:"role"`
	Text      string    `json:"text"`
	Source    string    `json:"source,omitempty"`
	Speaker   string    `json:"speaker,omitempty"`
	SegmentID string    `json:"segment_id,omitempty"`
	Final     bool      `json:"final"`
	CreatedAt time.Time `json:"created_at"`
}

type AssistState struct {
	FastText     string `json:"fast_text"`
	SlowText     string `json:"slow_text"`
	Streaming    bool   `json:"streaming"`
	GenerationID string `json:"generation_id,omitempty"`
	FastModel    string `json:"fast_model,omitempty"`
	SlowModel    string `json:"slow_model,omitempty"`
}

type SessionState struct {
	SessionID          string           `json:"session_id"`
	CreatedAt          time.Time        `json:"created_at"`
	UpdatedAt          time.Time        `json:"updated_at"`
	Messages           []Message        `json:"messages"`
	Transcript         []TranscriptItem `json:"transcript"`
	ClientPartial      string           `json:"client_partial"`
	SellerDraft        string           `json:"seller_draft"`
	SellerStreaming    bool             `json:"seller_streaming"`
	SellerGenerationID string           `json:"seller_generation_id"`
	Assist             AssistState      `json:"assist"`
	StageCandidate     *StageData       `json:"stage_candidate,omitempty"`
	StageCommitted     *StageData       `json:"stage_committed,omitempty"`
	Scorecard          *ScorecardData   `json:"scorecard,omitempty"`
	LastError          string           `json:"last_error,omitempty"`
	Events             []Event          `json:"events"`
}

type Store struct {
	mu        sync.RWMutex
	sessions  map[string]*SessionState
	seen      map[string]struct{}
	listeners map[string]map[chan Event]struct{}
}

func NewStore() *Store {
	return &Store{
		sessions:  make(map[string]*SessionState),
		seen:      make(map[string]struct{}),
		listeners: make(map[string]map[chan Event]struct{}),
	}
}

func (s *Store) Get(sessionID string) (SessionState, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	state, ok := s.sessions[sessionID]
	if !ok {
		return SessionState{}, false
	}
	return cloneState(*state), true
}

func (s *Store) Apply(event Event) SessionState {
	s.mu.Lock()
	defer s.mu.Unlock()

	state := s.ensureLocked(event.SessionID)
	if _, ok := s.seen[event.ID]; ok {
		return cloneState(*state)
	}
	s.seen[event.ID] = struct{}{}
	state.UpdatedAt = event.CreatedAt
	state.Events = append(state.Events, event)
	if len(state.Events) > 200 {
		state.Events = state.Events[len(state.Events)-200:]
	}

	switch event.Type {
	case EventSessionCreated:
	case EventSellerInput:
		if data, err := DecodeData[TextData](event); err == nil && data.Text != "" {
			state.Messages = append(state.Messages, Message{Role: "seller", Text: data.Text, CreatedAt: event.CreatedAt})
		}
	case EventClientPartial:
		if data, err := DecodeData[TextData](event); err == nil {
			state.ClientPartial = data.Text
		}
	case EventClientFinal:
		if data, err := DecodeData[TextData](event); err == nil {
			state.ClientPartial = ""
			if data.Text != "" {
				state.Messages = append(state.Messages, Message{Role: "client", Text: data.Text, CreatedAt: event.CreatedAt})
			}
		}
	case EventSTTPartial:
		if data, err := DecodeData[SpeechData](event); err == nil {
			if data.Role == "client" {
				state.ClientPartial = data.Text
			}
			state.Transcript = appendTranscript(state.Transcript, TranscriptItem{
				ID: event.ID, Role: data.Role, Text: data.Text, Source: data.Source, Speaker: data.Speaker, SegmentID: data.SegmentID, Final: false, CreatedAt: event.CreatedAt,
			})
		}
	case EventSTTFinal:
		if data, err := DecodeData[SpeechData](event); err == nil {
			if data.Role == "client" {
				state.ClientPartial = ""
				if data.Text != "" {
					state.Messages = append(state.Messages, Message{Role: "client", Text: data.Text, CreatedAt: event.CreatedAt})
				}
			} else if data.Role == "seller" && data.Text != "" {
				state.Messages = append(state.Messages, Message{Role: "seller", Text: data.Text, CreatedAt: event.CreatedAt})
			}
			state.Transcript = appendTranscript(state.Transcript, TranscriptItem{
				ID: event.ID, Role: data.Role, Text: data.Text, Source: data.Source, Speaker: data.Speaker, SegmentID: data.SegmentID, Final: true, CreatedAt: event.CreatedAt,
			})
		}
	case EventSellerStarted:
		if data, err := DecodeData[SellerStartedData](event); err == nil {
			state.SellerDraft = ""
			state.SellerStreaming = true
			state.SellerGenerationID = data.GenerationID
		}
	case EventSellerDelta:
		if data, err := DecodeData[SellerDeltaData](event); err == nil && data.GenerationID == state.SellerGenerationID {
			state.SellerDraft += data.Delta
			state.SellerStreaming = true
		}
	case EventSellerDone:
		if data, err := DecodeData[SellerDoneData](event); err == nil && data.GenerationID == state.SellerGenerationID {
			state.SellerDraft = data.Text
			state.SellerStreaming = false
		}
	case EventSellerCanceled:
		state.SellerStreaming = false
	case EventAssistStarted:
		if data, err := DecodeData[AssistStartedData](event); err == nil {
			state.Assist = AssistState{Streaming: true, GenerationID: data.GenerationID}
		}
	case EventAssistFastDone:
		if data, err := DecodeData[AssistFastDoneData](event); err == nil && data.GenerationID == state.Assist.GenerationID {
			state.Assist.FastText = data.Text
			state.Assist.FastModel = data.Model
			state.Assist.Streaming = true
		}
	case EventAssistDelta:
		if data, err := DecodeData[AssistDeltaData](event); err == nil && data.GenerationID == state.Assist.GenerationID {
			state.Assist.SlowText += data.Delta
			state.Assist.Streaming = true
		}
	case EventAssistDone:
		if data, err := DecodeData[AssistDoneData](event); err == nil && data.GenerationID == state.Assist.GenerationID {
			state.Assist.FastText = data.FastText
			state.Assist.SlowText = data.SlowText
			state.Assist.FastModel = data.FastModel
			state.Assist.SlowModel = data.SlowModel
			state.Assist.Streaming = false
		}
	case EventAssistCanceled:
		state.Assist.Streaming = false
	case EventStageCandidate:
		if data, err := DecodeData[StageData](event); err == nil {
			state.StageCandidate = &data
		}
	case EventStageCommitted:
		if data, err := DecodeData[StageData](event); err == nil {
			state.StageCommitted = &data
		}
	case EventScorecardUpdate:
		if data, err := DecodeData[ScorecardData](event); err == nil {
			state.Scorecard = &data
		}
	case EventError:
		if data, err := DecodeData[ErrorData](event); err == nil {
			state.LastError = data.Message
		}
	}

	for ch := range s.listeners[event.SessionID] {
		select {
		case ch <- event:
		default:
		}
	}
	return cloneState(*state)
}

func appendTranscript(items []TranscriptItem, item TranscriptItem) []TranscriptItem {
	if item.Role == "" {
		item.Role = "unknown"
	}
	if item.Text == "" {
		return items
	}
	for i := len(items) - 1; i >= 0; i-- {
		if !sameTranscriptStream(items[i], item) {
			continue
		}
		if !items[i].Final {
			item.ID = stableTranscriptID(items[i], item)
			item.CreatedAt = stableTranscriptCreatedAt(items[i], item)
			items[i] = item
			return items
		}
		if item.Final && items[i].Text == item.Text {
			item.ID = stableTranscriptID(items[i], item)
			item.CreatedAt = stableTranscriptCreatedAt(items[i], item)
			items[i] = item
			return items
		}
		break
	}
	items = append(items, item)
	if len(items) > 200 {
		items = items[len(items)-200:]
	}
	return items
}

func sameTranscriptStream(left, right TranscriptItem) bool {
	if left.SegmentID != "" || right.SegmentID != "" {
		return left.Role == right.Role &&
			left.Speaker == right.Speaker &&
			left.Source == right.Source &&
			left.SegmentID == right.SegmentID
	}
	return left.Role == right.Role &&
		left.Speaker == right.Speaker &&
		left.Source == right.Source
}

func stableTranscriptID(previous, next TranscriptItem) string {
	if previous.ID != "" {
		return previous.ID
	}
	return next.ID
}

func stableTranscriptCreatedAt(previous, next TranscriptItem) time.Time {
	if !previous.CreatedAt.IsZero() {
		return previous.CreatedAt
	}
	return next.CreatedAt
}

func (s *Store) Subscribe(sessionID string) (<-chan Event, func()) {
	ch := make(chan Event, 64)
	s.mu.Lock()
	if s.listeners[sessionID] == nil {
		s.listeners[sessionID] = make(map[chan Event]struct{})
	}
	s.listeners[sessionID][ch] = struct{}{}
	s.mu.Unlock()

	cancel := func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		if listeners := s.listeners[sessionID]; listeners != nil {
			delete(listeners, ch)
		}
		close(ch)
	}
	return ch, cancel
}

func (s *Store) ensureLocked(sessionID string) *SessionState {
	if state := s.sessions[sessionID]; state != nil {
		return state
	}
	now := time.Now().UTC()
	state := &SessionState{SessionID: sessionID, CreatedAt: now, UpdatedAt: now}
	s.sessions[sessionID] = state
	return state
}

func cloneState(state SessionState) SessionState {
	raw, _ := json.Marshal(state)
	var out SessionState
	_ = json.Unmarshal(raw, &out)
	return out
}
