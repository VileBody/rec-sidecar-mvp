package clean

import (
	"strings"
	"sync"
)

type sessionMemory struct {
	Messages            []Message
	ClientPartial       string
	CurrentStage        string
	SellerDraft         string
	SellerGenerationID  string
	LastSellerInput     string
	LastTriggerText     string
	StudentDirection    string
	StudentOriginals    []Message
	StudentTranslations []Message
	StudentAnswers      []Message
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
	if m.CurrentStage != "" {
		b.WriteString("\n--- Current stage ---\n")
		b.WriteString(m.CurrentStage)
		b.WriteString("\n")
	}
	return b.String()
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
	case EventStageCandidate, EventStageCommitted:
		if data, err := DecodeData[StageData](event); err == nil && data.Stage != "" {
			mem.CurrentStage = data.Stage
		}
	case EventSellerStarted:
		if data, err := DecodeData[SellerStartedData](event); err == nil {
			mem.SellerDraft = ""
			mem.SellerGenerationID = data.GenerationID
		}
	case EventSellerDelta:
		if data, err := DecodeData[SellerDeltaData](event); err == nil && data.GenerationID == mem.SellerGenerationID {
			mem.SellerDraft += data.Delta
		}
	case EventSellerDone:
		if data, err := DecodeData[SellerDoneData](event); err == nil && data.GenerationID == mem.SellerGenerationID {
			mem.SellerDraft = data.Text
			mem.SellerGenerationID = ""
		}
	case EventSellerCanceled:
		mem.SellerGenerationID = ""
	}
	return cloneSessionMemory(mem)
}

func cloneSessionMemory(mem *sessionMemory) *sessionMemory {
	copy := *mem
	copy.Messages = append([]Message(nil), mem.Messages...)
	copy.StudentOriginals = append([]Message(nil), mem.StudentOriginals...)
	copy.StudentTranslations = append([]Message(nil), mem.StudentTranslations...)
	copy.StudentAnswers = append([]Message(nil), mem.StudentAnswers...)
	return &copy
}
