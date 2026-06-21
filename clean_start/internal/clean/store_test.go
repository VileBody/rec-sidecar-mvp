package clean

import (
	"testing"
	"time"
)

func TestStoreApplyCreatesSessionAndDedupesEvents(t *testing.T) {
	store := NewStore()
	sessionID := "sess-store"
	event := NewEvent(sessionID, EventSessionCreated, "test", map[string]any{})

	state := store.Apply(event)
	if state.SessionID != sessionID {
		t.Fatalf("session id = %q", state.SessionID)
	}
	state = store.Apply(event)
	if len(state.Events) != 1 {
		t.Fatalf("duplicate event should not append, got %d events", len(state.Events))
	}
}

func TestStoreApplyUpdatesMessagesTranscriptStageScorecardAndError(t *testing.T) {
	store := NewStore()
	sessionID := "sess-flow"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSellerInput, "test", TextData{Text: "Здравствуйте"}))
	store.Apply(NewEvent(sessionID, EventSTTPartial, "test", SpeechData{Role: "client", Text: "Сомневаюсь", Source: "browser-system-audio", Speaker: "1", SegmentID: "seg-1"}))

	state := store.Apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{Role: "client", Text: "Сомневаюсь.", Source: "browser-system-audio", Speaker: "1", SegmentID: "seg-1"}))
	if state.ClientPartial != "" {
		t.Fatalf("client partial should clear after final, got %q", state.ClientPartial)
	}
	if len(state.Messages) != 2 || state.Messages[0].Role != "seller" || state.Messages[1].Role != "client" {
		t.Fatalf("messages not updated correctly: %#v", state.Messages)
	}
	if len(state.Transcript) != 1 || !state.Transcript[0].Final || state.Transcript[0].Text != "Сомневаюсь." {
		t.Fatalf("transcript final did not replace partial: %#v", state.Transcript)
	}

	confidence := 0.7
	stage := StageData{Stage: "S2.2", Title: "Квалификация", Confidence: &confidence}
	state = store.Apply(NewEvent(sessionID, EventStageCommitted, "test", stage))
	if state.StageCommitted == nil || state.StageCommitted.Stage != "S2.2" {
		t.Fatalf("stage committed not set: %#v", state.StageCommitted)
	}

	scorecard := ScorecardData{Readiness: "green", ReadinessLabel: "Зеленый", ReadyToAdvance: true, NextAction: "Переходить", Source: "test"}
	state = store.Apply(NewEvent(sessionID, EventScorecardUpdate, "test", scorecard))
	if state.Scorecard == nil || !state.Scorecard.ReadyToAdvance {
		t.Fatalf("scorecard not set: %#v", state.Scorecard)
	}

	state = store.Apply(NewEvent(sessionID, EventError, "test", ErrorData{Message: "boom"}))
	if state.LastError != "boom" {
		t.Fatalf("last error = %q", state.LastError)
	}
}

func TestStoreApplyIgnoresMismatchedGenerationEvents(t *testing.T) {
	store := NewStore()
	sessionID := "sess-generation"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-1"}))
	store.Apply(NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-old", Delta: "old"}))
	state := store.Apply(NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-1", Delta: "new"}))

	if state.SellerDraft != "new" {
		t.Fatalf("seller draft = %q, want new", state.SellerDraft)
	}
	state = store.Apply(NewEvent(sessionID, EventSellerDone, "test", SellerDoneData{GenerationID: "gen-old", Text: "old done"}))
	if state.SellerDraft != "new" || !state.SellerStreaming {
		t.Fatalf("mismatched done changed state: %#v", state)
	}
}

func TestStoreApplyStudentTranslationAndAnswer(t *testing.T) {
	store := NewStore()
	sessionID := "sess-student"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	state := store.Apply(NewEvent(sessionID, EventStudentInput, "test", StudentInputData{Text: "Hi, what happened?", Direction: StudentDirectionEnRu}))
	if state.Student.Direction != StudentDirectionEnRu {
		t.Fatalf("direction = %q", state.Student.Direction)
	}
	if len(state.Student.Originals) != 1 || state.Student.Originals[0].Text != "Hi, what happened?" {
		t.Fatalf("student originals = %#v", state.Student.Originals)
	}

	state = store.Apply(NewEvent(sessionID, EventStudentTranslateDone, "test", StudentTranslateDoneData{
		GenerationID:  "trn-1",
		SourceEventID: state.Student.Originals[0].ID,
		SourceText:    "Hi, what happened?",
		Text:          "Привет, что случилось?",
		Direction:     StudentDirectionEnRu,
		Provider:      "cerebras",
		Model:         "gpt-oss-120b",
	}))
	if len(state.Student.Translations) != 1 || state.Student.Translations[0].Text != "Привет, что случилось?" {
		t.Fatalf("student translations = %#v", state.Student.Translations)
	}

	store.Apply(NewEvent(sessionID, EventStudentAnswerStarted, "test", StudentAnswerStartedData{GenerationID: "stu-1", Trigger: "button"}))
	store.Apply(NewEvent(sessionID, EventStudentAnswerDelta, "test", StudentAnswerDeltaData{GenerationID: "stu-1", Delta: "Это вопрос"}))
	state = store.Apply(NewEvent(sessionID, EventStudentAnswerDone, "test", StudentAnswerDoneData{GenerationID: "stu-1", Text: "Это вопрос о событии.", Model: "gemini-3.5-flash"}))
	if state.Student.AnswerStreaming || state.Student.AnswerText != "Это вопрос о событии." || state.Student.AnswerModel != "gemini-3.5-flash" {
		t.Fatalf("student answer = %#v", state.Student)
	}
}

func TestStoreCloneSafety(t *testing.T) {
	store := NewStore()
	sessionID := "sess-clone"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSellerInput, "test", TextData{Text: "one"}))

	state, ok := store.Get(sessionID)
	if !ok {
		t.Fatal("session not found")
	}
	state.Messages[0].Text = "mutated"

	next, ok := store.Get(sessionID)
	if !ok {
		t.Fatal("session not found")
	}
	if next.Messages[0].Text != "one" {
		t.Fatalf("store leaked mutable slice: %#v", next.Messages)
	}
}

func TestStoreSubscribeReceivesEventsAndStopsAfterCancel(t *testing.T) {
	store := NewStore()
	sessionID := "sess-subscribe"
	events, cancel := store.Subscribe(sessionID)
	event := NewEvent(sessionID, EventSessionCreated, "test", map[string]any{})
	store.Apply(event)

	select {
	case got := <-events:
		if got.ID != event.ID {
			t.Fatalf("listener event id = %q, want %q", got.ID, event.ID)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for listener event")
	}

	cancel()
	cancel()
	select {
	case _, ok := <-events:
		if ok {
			t.Fatal("listener channel should close after cancel")
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for closed listener")
	}
}
