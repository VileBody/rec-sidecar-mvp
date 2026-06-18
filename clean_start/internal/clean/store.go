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

type SessionState struct {
	SessionID          string         `json:"session_id"`
	CreatedAt          time.Time      `json:"created_at"`
	UpdatedAt          time.Time      `json:"updated_at"`
	Messages           []Message      `json:"messages"`
	ClientPartial      string         `json:"client_partial"`
	SellerDraft        string         `json:"seller_draft"`
	SellerStreaming    bool           `json:"seller_streaming"`
	SellerGenerationID string         `json:"seller_generation_id"`
	StageCandidate     *StageData     `json:"stage_candidate,omitempty"`
	StageCommitted     *StageData     `json:"stage_committed,omitempty"`
	Scorecard          *ScorecardData `json:"scorecard,omitempty"`
	LastError          string         `json:"last_error,omitempty"`
	Events             []Event        `json:"events"`
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
