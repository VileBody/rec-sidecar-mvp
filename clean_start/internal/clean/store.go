package clean

import (
	"encoding/json"
	"strings"
	"sync"
	"time"
)

type Message struct {
	Role      string    `json:"role"`
	Text      string    `json:"text"`
	CreatedAt time.Time `json:"created_at"`
}

type TranscriptItem struct {
	ID         string    `json:"id,omitempty"`
	Role       string    `json:"role"`
	RoleReason string    `json:"role_reason,omitempty"`
	Text       string    `json:"text"`
	Source     string    `json:"source,omitempty"`
	Speaker    string    `json:"speaker,omitempty"`
	SegmentID  string    `json:"segment_id,omitempty"`
	Final      bool      `json:"final"`
	EchoReason string    `json:"echo_reason,omitempty"`
	EchoScore  float64   `json:"echo_score,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
}

type AssistState struct {
	FastText     string `json:"fast_text"`
	SlowText     string `json:"slow_text"`
	Streaming    bool   `json:"streaming"`
	GenerationID string `json:"generation_id,omitempty"`
	FastModel    string `json:"fast_model,omitempty"`
	SlowModel    string `json:"slow_model,omitempty"`
}

type InterviewLaneState struct {
	Question     string `json:"question,omitempty"`
	Text         string `json:"text"`
	Buffer       string `json:"-"`
	Streaming    bool   `json:"streaming"`
	GenerationID string `json:"generation_id,omitempty"`
	Status       string `json:"status,omitempty"`
	Error        string `json:"error,omitempty"`
	Provider     string `json:"provider,omitempty"`
	Model        string `json:"model,omitempty"`
}

type InterviewState struct {
	Question         string             `json:"question"`
	QuestionProvider string             `json:"question_provider,omitempty"`
	QuestionModel    string             `json:"question_model,omitempty"`
	Auto             InterviewLaneState `json:"auto"`
	Help             InterviewLaneState `json:"help"`
}

type StudentTranslationItem struct {
	ID            string    `json:"id"`
	SourceEventID string    `json:"source_event_id"`
	SourceText    string    `json:"source_text"`
	Text          string    `json:"text"`
	Direction     string    `json:"direction"`
	Provider      string    `json:"provider,omitempty"`
	Model         string    `json:"model,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
}

type StudentAnswerItem struct {
	ID                   string    `json:"id"`
	Role                 string    `json:"role"`
	Text                 string    `json:"text"`
	Trigger              string    `json:"trigger,omitempty"`
	Model                string    `json:"model,omitempty"`
	Streaming            bool      `json:"streaming,omitempty"`
	TranslationText      string    `json:"translation_text,omitempty"`
	TranslationDirection string    `json:"translation_direction,omitempty"`
	TranslationProvider  string    `json:"translation_provider,omitempty"`
	TranslationModel     string    `json:"translation_model,omitempty"`
	TranslationStreaming bool      `json:"translation_streaming,omitempty"`
	CreatedAt            time.Time `json:"created_at"`
}

type StudentState struct {
	Direction               string                   `json:"direction"`
	Originals               []TranscriptItem         `json:"originals"`
	Self                    []TranscriptItem         `json:"self"`
	Translations            []StudentTranslationItem `json:"translations"`
	AnswerItems             []StudentAnswerItem      `json:"answer_items"`
	TranslationStreaming    bool                     `json:"translation_streaming"`
	TranslationGenerationID string                   `json:"translation_generation_id,omitempty"`
	AnswerText              string                   `json:"answer_text"`
	AnswerStreaming         bool                     `json:"answer_streaming"`
	AnswerGenerationID      string                   `json:"answer_generation_id,omitempty"`
	AnswerModel             string                   `json:"answer_model,omitempty"`
}

type SessionState struct {
	SessionID                   string           `json:"session_id"`
	CreatedAt                   time.Time        `json:"created_at"`
	UpdatedAt                   time.Time        `json:"updated_at"`
	Messages                    []Message        `json:"messages"`
	Transcript                  []TranscriptItem `json:"transcript"`
	ClientPartial               string           `json:"client_partial"`
	SellerDraft                 string           `json:"seller_draft"`
	SellerDraftBuffer           string           `json:"-"`
	SellerStreaming             bool             `json:"seller_streaming"`
	SellerGenerationID          string           `json:"seller_generation_id"`
	SellerDraftImmediate        string           `json:"seller_draft_immediate"`
	SellerDraftImmediateBuffer  string           `json:"-"`
	SellerImmediateStreaming    bool             `json:"seller_immediate_streaming"`
	SellerImmediateGenerationID string           `json:"seller_immediate_generation_id"`
	Assist                      AssistState      `json:"assist"`
	Interview                   InterviewState   `json:"interview"`
	Student                     StudentState     `json:"student"`
	StageCandidate              *StageData       `json:"stage_candidate,omitempty"`
	StageCommitted              *StageData       `json:"stage_committed,omitempty"`
	Scorecard                   *ScorecardData   `json:"scorecard,omitempty"`
	LastError                   string           `json:"last_error,omitempty"`
	Events                      []Event          `json:"events"`
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
	if event.Type == EventClientTelemetry {
		return cloneState(*state)
	}
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
		if state.Student.Direction == "" {
			state.Student.Direction = "en-ru"
		}
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
			item := transcriptItemFromSpeech(event, data, false)
			state.Transcript = appendTranscript(state.Transcript, item)
			switch data.Role {
			case "student_original":
				state.Student.Originals = appendTranscript(state.Student.Originals, item)
			case "student_self":
				state.Student.Self = appendTranscript(state.Student.Self, item)
			}
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
			item := transcriptItemFromSpeech(event, data, true)
			state.Transcript = appendTranscript(state.Transcript, item)
			switch data.Role {
			case "student_original":
				state.Student.Originals = appendTranscript(state.Student.Originals, item)
			case "student_self":
				state.Student.Self = appendTranscript(state.Student.Self, item)
			}
		}
	case EventSellerStarted:
		if data, err := DecodeData[SellerStartedData](event); err == nil {
			if isManualSellerTrigger(data.Trigger) {
				state.SellerDraftImmediate = ""
				state.SellerDraftImmediateBuffer = ""
				state.SellerImmediateStreaming = true
				state.SellerImmediateGenerationID = data.GenerationID
			} else {
				state.SellerDraftBuffer = ""
				state.SellerStreaming = true
				state.SellerGenerationID = data.GenerationID
			}
		}
	case EventSellerDelta:
		if data, err := DecodeData[SellerDeltaData](event); err == nil {
			switch data.GenerationID {
			case state.SellerImmediateGenerationID:
				state.SellerDraftImmediateBuffer += data.Delta
				state.SellerDraftImmediate = state.SellerDraftImmediateBuffer
				state.SellerImmediateStreaming = true
			case state.SellerGenerationID:
				state.SellerDraftBuffer += data.Delta
				state.SellerDraft = state.SellerDraftBuffer
				state.SellerStreaming = true
			}
		}
	case EventSellerDone:
		if data, err := DecodeData[SellerDoneData](event); err == nil {
			switch data.GenerationID {
			case state.SellerImmediateGenerationID:
				state.SellerDraftImmediate = data.Text
				state.SellerDraftImmediateBuffer = ""
				state.SellerImmediateStreaming = false
			case state.SellerGenerationID:
				state.SellerDraft = data.Text
				state.SellerDraftBuffer = ""
				state.SellerStreaming = false
			}
		}
	case EventSellerCanceled:
		state.SellerStreaming = false
		state.SellerImmediateStreaming = false
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
	case EventInterviewAutoStarted:
		if data, err := DecodeData[InterviewStartedData](event); err == nil {
			state.Interview.Auto.Buffer = ""
			state.Interview.Auto.Streaming = true
			state.Interview.Auto.GenerationID = data.GenerationID
			state.Interview.Auto.Status = "identifying"
			state.Interview.Auto.Error = ""
		}
	case EventInterviewQuestionIdentified:
		if data, err := DecodeData[InterviewQuestionIdentifiedData](event); err == nil && data.GenerationID == state.Interview.Auto.GenerationID {
			state.Interview.Question = data.Question
			state.Interview.Auto.Question = data.Question
			state.Interview.QuestionProvider = data.Provider
			state.Interview.QuestionModel = data.Model
			state.Interview.Auto.Status = "streaming"
		}
	case EventInterviewAutoDelta:
		if data, err := DecodeData[InterviewDeltaData](event); err == nil && data.GenerationID == state.Interview.Auto.GenerationID {
			state.Interview.Auto.Buffer += data.Delta
			state.Interview.Auto.Text = state.Interview.Auto.Buffer
			state.Interview.Auto.Streaming = true
			state.Interview.Auto.Status = "streaming"
		}
	case EventInterviewAutoDone:
		if data, err := DecodeData[InterviewDoneData](event); err == nil && data.GenerationID == state.Interview.Auto.GenerationID {
			state.Interview.Question = data.Question
			state.Interview.Auto.Question = data.Question
			state.Interview.Auto.Text = data.Text
			state.Interview.Auto.Buffer = ""
			state.Interview.Auto.Streaming = false
			state.Interview.Auto.Status = "ready"
			state.Interview.Auto.Provider = data.Provider
			state.Interview.Auto.Model = data.Model
		}
	case EventInterviewAutoCanceled:
		if data, err := DecodeData[InterviewCanceledData](event); err == nil && data.GenerationID == state.Interview.Auto.GenerationID {
			state.Interview.Auto.Buffer = ""
			state.Interview.Auto.Streaming = false
			state.Interview.Auto.Status = data.Reason
		}
	case EventInterviewHelpStarted:
		if data, err := DecodeData[InterviewStartedData](event); err == nil {
			state.Interview.Help.Buffer = ""
			state.Interview.Help.Question = data.Question
			state.Interview.Help.Streaming = true
			state.Interview.Help.GenerationID = data.GenerationID
			state.Interview.Help.Status = "streaming"
			state.Interview.Help.Error = ""
		}
	case EventInterviewHelpDelta:
		if data, err := DecodeData[InterviewDeltaData](event); err == nil && data.GenerationID == state.Interview.Help.GenerationID {
			state.Interview.Help.Buffer += data.Delta
			state.Interview.Help.Text = state.Interview.Help.Buffer
			state.Interview.Help.Streaming = true
			state.Interview.Help.Status = "streaming"
		}
	case EventInterviewHelpDone:
		if data, err := DecodeData[InterviewDoneData](event); err == nil && data.GenerationID == state.Interview.Help.GenerationID {
			state.Interview.Help.Question = data.Question
			state.Interview.Help.Text = data.Text
			state.Interview.Help.Buffer = ""
			state.Interview.Help.Streaming = false
			state.Interview.Help.Status = "ready"
			state.Interview.Help.Provider = data.Provider
			state.Interview.Help.Model = data.Model
		}
	case EventInterviewHelpCanceled:
		if data, err := DecodeData[InterviewCanceledData](event); err == nil && data.GenerationID == state.Interview.Help.GenerationID {
			state.Interview.Help.Buffer = ""
			state.Interview.Help.Streaming = false
			state.Interview.Help.Status = data.Reason
		}
	case EventStudentDirection:
		if data, err := DecodeData[StudentDirectionData](event); err == nil && data.Direction != "" {
			state.Student.Direction = data.Direction
		}
	case EventStudentInput:
		if data, err := DecodeData[StudentInputData](event); err == nil && data.Text != "" {
			if data.Direction != "" {
				state.Student.Direction = data.Direction
			}
			state.Student.Originals = appendTranscript(state.Student.Originals, TranscriptItem{
				ID: event.ID, Role: "student_original", Text: data.Text, Source: "manual", Final: true, CreatedAt: event.CreatedAt,
			})
		}
	case EventStudentTranslateStarted:
		if data, err := DecodeData[StudentTranslateStartedData](event); err == nil {
			state.Student.TranslationStreaming = true
			state.Student.TranslationGenerationID = data.GenerationID
		}
	case EventStudentTranslateDone:
		if data, err := DecodeData[StudentTranslateDoneData](event); err == nil {
			state.Student.TranslationStreaming = false
			state.Student.TranslationGenerationID = data.GenerationID
			state.Student.Translations = appendStudentTranslation(state.Student.Translations, StudentTranslationItem{
				ID: data.GenerationID, SourceEventID: data.SourceEventID, SourceText: data.SourceText, Text: data.Text, Direction: data.Direction, Provider: data.Provider, Model: data.Model, CreatedAt: event.CreatedAt,
			})
		}
	case EventStudentAnswerRequest:
		if data, err := DecodeData[StudentAnswerRequestData](event); err == nil {
			state.Student.AnswerItems = appendStudentAnswerItem(state.Student.AnswerItems, StudentAnswerItem{
				ID:        event.ID,
				Role:      "user",
				Text:      studentAnswerRequestText(data),
				Trigger:   data.Trigger,
				CreatedAt: event.CreatedAt,
			})
		}
	case EventStudentAnswerStarted:
		if data, err := DecodeData[StudentAnswerStartedData](event); err == nil {
			state.Student.AnswerText = ""
			state.Student.AnswerStreaming = true
			state.Student.AnswerGenerationID = data.GenerationID
			state.Student.AnswerModel = ""
			state.Student.AnswerItems = appendStudentAnswerItem(state.Student.AnswerItems, StudentAnswerItem{
				ID:        data.GenerationID,
				Role:      "assistant",
				Trigger:   data.Trigger,
				Streaming: true,
				CreatedAt: event.CreatedAt,
			})
		}
	case EventStudentAnswerDelta:
		if data, err := DecodeData[StudentAnswerDeltaData](event); err == nil && data.GenerationID == state.Student.AnswerGenerationID {
			state.Student.AnswerText += data.Delta
			state.Student.AnswerStreaming = true
			state.Student.AnswerItems = appendStudentAnswerDelta(state.Student.AnswerItems, data.GenerationID, data.Delta)
		}
	case EventStudentAnswerDone:
		if data, err := DecodeData[StudentAnswerDoneData](event); err == nil && data.GenerationID == state.Student.AnswerGenerationID {
			state.Student.AnswerText = data.Text
			state.Student.AnswerModel = data.Model
			state.Student.AnswerStreaming = false
			state.Student.AnswerItems = finishStudentAnswerItem(state.Student.AnswerItems, data.GenerationID, data.Text, data.Model)
		}
	case EventStudentAnswerCanceled:
		state.Student.AnswerStreaming = false
		state.Student.AnswerItems = cancelStudentAnswerItem(state.Student.AnswerItems, state.Student.AnswerGenerationID)
	case EventStudentAnswerTranslateStarted:
		if data, err := DecodeData[StudentAnswerTranslateStartedData](event); err == nil {
			state.Student.AnswerItems = startStudentAnswerTranslation(state.Student.AnswerItems, data.GenerationID, data.Direction)
		}
	case EventStudentAnswerTranslateDone:
		if data, err := DecodeData[StudentAnswerTranslateDoneData](event); err == nil {
			state.Student.AnswerItems = finishStudentAnswerTranslation(state.Student.AnswerItems, data.GenerationID, data.Text, data.Direction, data.Provider, data.Model)
		}
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
			switch data.Where {
			case "interview.question", "interview.auto":
				state.Interview.Auto.Status = "error"
				state.Interview.Auto.Error = data.Message
			case "interview.help":
				state.Interview.Help.Status = "error"
				state.Interview.Help.Error = data.Message
			}
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

func appendStudentTranslation(items []StudentTranslationItem, item StudentTranslationItem) []StudentTranslationItem {
	if strings.TrimSpace(item.Text) == "" {
		return items
	}
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].SourceEventID == item.SourceEventID && item.SourceEventID != "" {
			items[i] = item
			return items
		}
	}
	return append(items, item)
}

func studentAnswerRequestText(data StudentAnswerRequestData) string {
	text := strings.TrimSpace(data.Text)
	if text != "" {
		return text
	}
	return "Помоги по последнему фрагменту"
}

func appendStudentAnswerItem(items []StudentAnswerItem, item StudentAnswerItem) []StudentAnswerItem {
	item.Text = strings.TrimSpace(item.Text)
	if item.Role == "" {
		item.Role = "assistant"
	}
	items = append(items, item)
	if len(items) > 80 {
		items = items[len(items)-80:]
	}
	return items
}

func appendStudentAnswerDelta(items []StudentAnswerItem, generationID, delta string) []StudentAnswerItem {
	if delta == "" {
		return items
	}
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].ID == generationID {
			items[i].Text += delta
			items[i].Streaming = true
			return items
		}
	}
	return appendStudentAnswerItem(items, StudentAnswerItem{ID: generationID, Role: "assistant", Text: delta, Streaming: true, CreatedAt: time.Now().UTC()})
}

func finishStudentAnswerItem(items []StudentAnswerItem, generationID, text, model string) []StudentAnswerItem {
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].ID == generationID {
			items[i].Text = strings.TrimSpace(text)
			items[i].Model = model
			items[i].Streaming = false
			return items
		}
	}
	return appendStudentAnswerItem(items, StudentAnswerItem{ID: generationID, Role: "assistant", Text: text, Model: model, CreatedAt: time.Now().UTC()})
}

func cancelStudentAnswerItem(items []StudentAnswerItem, generationID string) []StudentAnswerItem {
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].ID == generationID {
			if strings.TrimSpace(items[i].Text) == "" {
				return append(items[:i], items[i+1:]...)
			}
			items[i].Streaming = false
			return items
		}
	}
	return items
}

func startStudentAnswerTranslation(items []StudentAnswerItem, generationID, direction string) []StudentAnswerItem {
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].ID == generationID {
			items[i].TranslationDirection = direction
			items[i].TranslationStreaming = true
			return items
		}
	}
	return items
}

func finishStudentAnswerTranslation(items []StudentAnswerItem, generationID, text, direction, provider, model string) []StudentAnswerItem {
	text = strings.TrimSpace(text)
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].ID == generationID {
			items[i].TranslationText = text
			items[i].TranslationDirection = direction
			items[i].TranslationProvider = provider
			items[i].TranslationModel = model
			items[i].TranslationStreaming = false
			return items
		}
	}
	return items
}

func transcriptItemFromSpeech(event Event, data SpeechData, final bool) TranscriptItem {
	return TranscriptItem{
		ID:         event.ID,
		Role:       data.Role,
		RoleReason: data.RoleReason,
		Text:       data.Text,
		Source:     data.Source,
		Speaker:    data.Speaker,
		SegmentID:  data.SegmentID,
		Final:      final,
		EchoReason: data.EchoReason,
		EchoScore:  data.EchoScore,
		CreatedAt:  event.CreatedAt,
	}
}

func appendTranscript(items []TranscriptItem, item TranscriptItem) []TranscriptItem {
	if item.Role == "" {
		item.Role = "unknown"
	}
	if item.Text == "" {
		return items
	}
	for i := len(items) - 1; i >= 0; i-- {
		if sameTranscriptStream(items[i], item) && (!items[i].Final || item.Final) {
			item.ID = stableTranscriptID(items[i], item)
			item.CreatedAt = stableTranscriptCreatedAt(items[i], item)
			items[i] = item
			return items
		}
		if shouldReplaceEquivalentPartial(items[i], item) {
			item.ID = stableTranscriptID(items[i], item)
			item.CreatedAt = stableTranscriptCreatedAt(items[i], item)
			items[i] = item
			return items
		}
	}
	items = append(items, item)
	if len(items) > 200 {
		items = items[len(items)-200:]
	}
	return items
}

func shouldReplaceEquivalentPartial(previous, next TranscriptItem) bool {
	if previous.Final || !next.Final {
		return false
	}
	if !sameTranscriptSpeakerSource(previous, next) {
		return false
	}
	if !equivalentTranscriptText(previous.Text, next.Text) {
		return false
	}
	if previous.CreatedAt.IsZero() || next.CreatedAt.IsZero() {
		return true
	}
	delta := next.CreatedAt.Sub(previous.CreatedAt)
	if delta < 0 {
		delta = -delta
	}
	return delta <= 20*time.Second
}

func sameTranscriptSpeakerSource(left, right TranscriptItem) bool {
	return left.Role == right.Role &&
		left.Speaker == right.Speaker &&
		left.Source == right.Source
}

func sameTranscriptStream(left, right TranscriptItem) bool {
	if left.SegmentID != "" || right.SegmentID != "" {
		return sameTranscriptSpeakerSource(left, right) &&
			left.SegmentID == right.SegmentID
	}
	return sameTranscriptSpeakerSource(left, right)
}

func equivalentTranscriptText(left, right string) bool {
	return normalizeTranscriptText(left) == normalizeTranscriptText(right)
}

func normalizeTranscriptText(text string) string {
	text = strings.ToLower(strings.Join(strings.Fields(text), " "))
	return strings.Trim(text, " \t\r\n.!?,:;…")
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

	var once sync.Once
	cancel := func() {
		once.Do(func() {
			s.mu.Lock()
			defer s.mu.Unlock()
			if listeners := s.listeners[sessionID]; listeners != nil {
				delete(listeners, ch)
			}
			close(ch)
		})
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
