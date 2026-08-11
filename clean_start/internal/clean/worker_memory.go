package clean

import (
	"encoding/json"
	"strings"
	"sync"
)

type sessionMemory struct {
	Messages                    []Message
	ClientPartial               string
	CurrentStage                string
	CurrentStageData            *StageData
	CurrentScorecard            *ScorecardData
	SellerDraft                 string
	SellerDraftBuffer           string
	SellerGenerationID          string
	SellerImmediateGenerationID string
	LastSellerInput             string
	LastTriggerText             string
	StudentDirection            string
	StudentOriginals            []Message
	StudentTranslations         []Message
	StudentAnswers              []Message
	InterviewQuestion           string
}

func (m *sessionMemory) contextBlock() string {
	var b strings.Builder
	b.WriteString("Живой high-check B2C sales разговор.\n\n--- Диалог ---\n")
	if len(m.Messages) == 0 && m.ClientPartial == "" {
		b.WriteString("(диалог пока не начался)\n")
	}
	for _, msg := range m.Messages {
		role := "Client"
		if msg.Role == "seller" {
			role = "Seller"
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	if m.ClientPartial != "" {
		b.WriteString("Client partial: ")
		b.WriteString(m.ClientPartial)
		b.WriteString("\n")
	}
	m.writeSalesGuidanceBlock(&b)
	return b.String()
}

func (m *sessionMemory) writeSalesGuidanceBlock(b *strings.Builder) {
	stage := strings.TrimSpace(m.CurrentStage)
	if m.CurrentStageData != nil && strings.TrimSpace(m.CurrentStageData.Stage) != "" {
		stage = strings.TrimSpace(m.CurrentStageData.Stage)
	}
	if stage == "" {
		return
	}
	b.WriteString("\n--- Current stage / agenda ---\n")
	b.WriteString("Stage: ")
	b.WriteString(stage)
	b.WriteString("\n")
	if data := m.CurrentStageData; data != nil {
		writeContextLine(b, "Title", data.Title)
		writeContextLine(b, "Agenda", data.Agenda)
		writeContextLine(b, "Emotional intent", data.Emotion)
		writeContextLine(b, "Canonical next step", data.Step)
	}
	if score := m.CurrentScorecard; score != nil {
		b.WriteString("\n--- Current scorecard ---\n")
		writeContextLine(b, "Readiness", score.Readiness)
		writeContextLine(b, "Readiness label", score.ReadinessLabel)
		writeContextLine(b, "Summary", score.Summary)
		writeContextLine(b, "Recommended next action", score.NextAction)
		if score.ReadyToAdvance {
			b.WriteString("Ready to advance: yes\n")
		} else {
			b.WriteString("Ready to advance: no\n")
		}
		if raw := compactRawJSON(score.Raw, 2200); raw != "" {
			b.WriteString("Raw scorecard: ")
			b.WriteString(raw)
			b.WriteString("\n")
		}
	} else if m.CurrentStageData != nil {
		if raw := compactRawJSON(m.CurrentStageData.Scorecard, 2200); raw != "" {
			b.WriteString("\n--- Current scorecard ---\n")
			b.WriteString("Raw scorecard: ")
			b.WriteString(raw)
			b.WriteString("\n")
		}
	}
}

func writeContextLine(b *strings.Builder, label, value string) {
	value = strings.TrimSpace(value)
	if value == "" {
		return
	}
	b.WriteString(label)
	b.WriteString(": ")
	b.WriteString(value)
	b.WriteString("\n")
}

func compactRawJSON(raw json.RawMessage, maxRunes int) string {
	if len(raw) == 0 || string(raw) == "null" {
		return ""
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return truncateRunes(strings.Join(strings.Fields(string(raw)), " "), maxRunes)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return truncateRunes(strings.Join(strings.Fields(string(raw)), " "), maxRunes)
	}
	return truncateRunes(string(encoded), maxRunes)
}

func truncateRunes(text string, maxRunes int) string {
	if maxRunes <= 0 {
		return ""
	}
	runes := []rune(text)
	if len(runes) <= maxRunes {
		return text
	}
	return string(runes[:maxRunes]) + "..."
}

func (m *sessionMemory) studentContextBlock() string {
	var b strings.Builder
	b.WriteString("Student translation/help session.\n")
	b.WriteString("Direction: ")
	if m.StudentDirection == "" {
		b.WriteString("en-ru")
	} else {
		b.WriteString(m.StudentDirection)
	}
	b.WriteString("\n\n--- Original transcript ---\n")
	if len(m.StudentOriginals) == 0 {
		b.WriteString("(empty)\n")
	}
	for _, msg := range m.StudentOriginals {
		b.WriteString("- ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	b.WriteString("\n--- Translation ---\n")
	if len(m.StudentTranslations) == 0 {
		b.WriteString("(empty)\n")
	}
	for _, msg := range m.StudentTranslations {
		b.WriteString("- ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	b.WriteString("\n--- Help/chat history ---\n")
	if len(m.StudentAnswers) == 0 {
		b.WriteString("(empty)\n")
	}
	for _, msg := range m.StudentAnswers {
		role := "Assistant"
		if msg.Role == "user" {
			role = "User"
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	return b.String()
}

func (m *sessionMemory) interviewContextBlock() string {
	var b strings.Builder
	b.WriteString("Live job interview for Kirill Ergin. Interviewer speech comes from system audio; Kirill's speech comes from the microphone.\n\n--- Recent interview transcript ---\n")
	start := len(m.Messages) - 48
	if start < 0 {
		start = 0
	}
	if start == len(m.Messages) {
		b.WriteString("(interview transcript is empty)\n")
	}
	for _, msg := range m.Messages[start:] {
		role := "Interviewer"
		if msg.Role == "seller" {
			role = "Kirill"
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	if question := strings.TrimSpace(m.InterviewQuestion); question != "" {
		b.WriteString("\n--- Last identified interviewer question ---\n")
		b.WriteString(question)
		b.WriteString("\n")
	}
	return tailRunes(b.String(), 18000)
}

func (m *sessionMemory) latestInterviewerText() string {
	for i := len(m.Messages) - 1; i >= 0; i-- {
		if m.Messages[i].Role == "client" && strings.TrimSpace(m.Messages[i].Text) != "" {
			return strings.TrimSpace(m.Messages[i].Text)
		}
	}
	return ""
}

func tailRunes(text string, maxRunes int) string {
	runes := []rune(text)
	if maxRunes <= 0 || len(runes) <= maxRunes {
		return text
	}
	return "[earlier transcript omitted]\n" + string(runes[len(runes)-maxRunes:])
}

type memoryBook struct {
	mu       sync.Mutex
	sessions map[string]*sessionMemory
	seen     map[string]struct{}
}

func newMemoryBook() *memoryBook {
	return &memoryBook{
		sessions: make(map[string]*sessionMemory),
		seen:     make(map[string]struct{}),
	}
}

func (b *memoryBook) apply(event Event) *sessionMemory {
	b.mu.Lock()
	defer b.mu.Unlock()
	mem := b.sessions[event.SessionID]
	if mem == nil {
		mem = &sessionMemory{CurrentStage: "S2.1"}
		b.sessions[event.SessionID] = mem
	}
	if _, ok := b.seen[event.ID]; ok {
		return cloneSessionMemory(mem)
	}
	b.seen[event.ID] = struct{}{}
	switch event.Type {
	case EventPersonalReset:
		studentDirection := mem.StudentDirection
		*mem = sessionMemory{CurrentStage: "S2.1", StudentDirection: studentDirection}
	case EventSellerInput:
		if data, err := DecodeData[TextData](event); err == nil && data.Text != "" {
			mem.Messages = append(mem.Messages, Message{Role: "seller", Text: data.Text, CreatedAt: event.CreatedAt})
			mem.LastSellerInput = data.Text
		}
	case EventClientPartial:
		if data, err := DecodeData[TextData](event); err == nil {
			mem.ClientPartial = data.Text
		}
	case EventClientFinal:
		if data, err := DecodeData[TextData](event); err == nil && data.Text != "" {
			mem.Messages = append(mem.Messages, Message{Role: "client", Text: data.Text, CreatedAt: event.CreatedAt})
			mem.ClientPartial = ""
		}
	case EventSTTPartial:
		if data, err := DecodeData[SpeechData](event); err == nil && data.Role == "client" {
			mem.ClientPartial = data.Text
		}
	case EventSTTFinal:
		if data, err := DecodeData[SpeechData](event); err == nil && data.Text != "" {
			switch data.Role {
			case "seller":
				mem.Messages = append(mem.Messages, Message{Role: "seller", Text: data.Text, CreatedAt: event.CreatedAt})
				mem.LastSellerInput = data.Text
			case "client":
				mem.Messages = append(mem.Messages, Message{Role: "client", Text: data.Text, CreatedAt: event.CreatedAt})
				mem.ClientPartial = ""
			case "student_original":
				mem.StudentOriginals = append(mem.StudentOriginals, Message{Role: "student_original", Text: data.Text, CreatedAt: event.CreatedAt})
			}
		}
	case EventStudentDirection:
		if data, err := DecodeData[StudentDirectionData](event); err == nil && data.Direction != "" {
			mem.StudentDirection = data.Direction
		}
	case EventStudentInput:
		if data, err := DecodeData[StudentInputData](event); err == nil && data.Text != "" {
			if data.Direction != "" {
				mem.StudentDirection = data.Direction
			}
			mem.StudentOriginals = append(mem.StudentOriginals, Message{Role: "student_original", Text: data.Text, CreatedAt: event.CreatedAt})
		}
	case EventStudentTranslateDone:
		if data, err := DecodeData[StudentTranslateDoneData](event); err == nil && data.Text != "" {
			mem.StudentTranslations = append(mem.StudentTranslations, Message{Role: "translation", Text: data.Text, CreatedAt: event.CreatedAt})
		}
	case EventStudentAnswerRequest:
		if data, err := DecodeData[StudentAnswerRequestData](event); err == nil {
			mem.StudentAnswers = append(mem.StudentAnswers, Message{Role: "user", Text: studentAnswerRequestText(data), CreatedAt: event.CreatedAt})
		}
	case EventStudentAnswerDone:
		if data, err := DecodeData[StudentAnswerDoneData](event); err == nil && data.Text != "" {
			mem.StudentAnswers = append(mem.StudentAnswers, Message{Role: "assistant", Text: data.Text, CreatedAt: event.CreatedAt})
		}
	case EventInterviewQuestionIdentified:
		if data, err := DecodeData[InterviewQuestionIdentifiedData](event); err == nil && data.Question != "" {
			mem.InterviewQuestion = data.Question
		}
	case EventStageCandidate, EventStageCommitted:
		if data, err := DecodeData[StageData](event); err == nil && data.Stage != "" {
			mem.CurrentStage = data.Stage
			stageCopy := cloneStageData(data)
			mem.CurrentStageData = &stageCopy
		}
	case EventScorecardUpdate:
		if data, err := DecodeData[ScorecardData](event); err == nil {
			scoreCopy := cloneScorecardData(data)
			mem.CurrentScorecard = &scoreCopy
		}
	case EventSellerStarted:
		if data, err := DecodeData[SellerStartedData](event); err == nil {
			if isManualSellerTrigger(data.Trigger) {
				mem.SellerImmediateGenerationID = data.GenerationID
			} else {
				mem.SellerDraftBuffer = ""
				mem.SellerGenerationID = data.GenerationID
			}
		}
	case EventSellerDelta:
		if data, err := DecodeData[SellerDeltaData](event); err == nil && data.GenerationID == mem.SellerGenerationID {
			mem.SellerDraftBuffer += data.Delta
			mem.SellerDraft = mem.SellerDraftBuffer
		}
	case EventSellerDone:
		if data, err := DecodeData[SellerDoneData](event); err == nil {
			switch data.GenerationID {
			case mem.SellerImmediateGenerationID:
				mem.SellerImmediateGenerationID = ""
			case mem.SellerGenerationID:
				mem.SellerDraft = data.Text
				mem.SellerDraftBuffer = ""
				mem.SellerGenerationID = ""
			}
		}
	case EventSellerCanceled:
		mem.SellerGenerationID = ""
		mem.SellerImmediateGenerationID = ""
	}
	return cloneSessionMemory(mem)
}

func cloneSessionMemory(mem *sessionMemory) *sessionMemory {
	copy := *mem
	copy.Messages = append([]Message(nil), mem.Messages...)
	copy.StudentOriginals = append([]Message(nil), mem.StudentOriginals...)
	copy.StudentTranslations = append([]Message(nil), mem.StudentTranslations...)
	copy.StudentAnswers = append([]Message(nil), mem.StudentAnswers...)
	if mem.CurrentStageData != nil {
		stageCopy := cloneStageData(*mem.CurrentStageData)
		copy.CurrentStageData = &stageCopy
	}
	if mem.CurrentScorecard != nil {
		scoreCopy := cloneScorecardData(*mem.CurrentScorecard)
		copy.CurrentScorecard = &scoreCopy
	}
	return &copy
}

func cloneStageData(data StageData) StageData {
	data.Scorecard = append(json.RawMessage(nil), data.Scorecard...)
	return data
}

func cloneScorecardData(data ScorecardData) ScorecardData {
	data.Raw = append(json.RawMessage(nil), data.Raw...)
	return data
}
