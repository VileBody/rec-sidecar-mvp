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

func TestStoreApplyKeepsPreviousSellerDraftUntilNextDelta(t *testing.T) {
	store := NewStore()
	sessionID := "sess-preserve-draft"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-opener", Trigger: "opener"}))
	state := store.Apply(NewEvent(sessionID, EventSellerDone, "test", SellerDoneData{GenerationID: "gen-opener", Text: "Открывашка"}))
	if state.SellerDraft != "Открывашка" {
		t.Fatalf("seller draft = %q, want opener", state.SellerDraft)
	}

	state = store.Apply(NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-next", Trigger: "client_final"}))
	if state.SellerDraft != "Открывашка" || !state.SellerStreaming {
		t.Fatalf("start should preserve visible draft while streaming: %#v", state)
	}

	state = store.Apply(NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-next", Delta: "Новая"}))
	if state.SellerDraft != "Новая" {
		t.Fatalf("first delta should replace visible draft, got %q", state.SellerDraft)
	}
}

func TestStoreApplySeparatesImmediateSellerDraft(t *testing.T) {
	store := NewStore()
	sessionID := "sess-immediate-generation"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-auto", Trigger: "client_final"}))
	store.Apply(NewEvent(sessionID, EventSellerDone, "test", SellerDoneData{GenerationID: "gen-auto", Text: "Авто-реплика", Provider: "vertex", Model: "gemini"}))

	store.Apply(NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-manual", Trigger: "manual_generate"}))
	store.Apply(NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-manual", Delta: "Ручная"}))
	state := store.Apply(NewEvent(sessionID, EventSellerDone, "test", SellerDoneData{GenerationID: "gen-manual", Text: "Ручная реплика", Provider: "vertex", Model: "gemini"}))

	if state.SellerDraft != "Авто-реплика" {
		t.Fatalf("seller draft = %q, want auto draft preserved", state.SellerDraft)
	}
	if state.SellerDraftImmediate != "Ручная реплика" {
		t.Fatalf("immediate draft = %q, want manual draft", state.SellerDraftImmediate)
	}
	if state.SellerStreaming || state.SellerImmediateStreaming {
		t.Fatalf("streaming flags left on: auto=%v immediate=%v", state.SellerStreaming, state.SellerImmediateStreaming)
	}
}

func TestStoreApplyKeepsInterviewAutoAndHelpIndependent(t *testing.T) {
	store := NewStore()
	sessionID := "sess-interview-independent"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventInterviewAutoStarted, "test", InterviewStartedData{GenerationID: "auto-1", Trigger: "question", Question: "Tell me about yourself."}))
	store.Apply(NewEvent(sessionID, EventInterviewQuestionIdentified, "test", InterviewQuestionIdentifiedData{GenerationID: "auto-1", Question: "Tell me about yourself.", Provider: "openrouter", Model: "gemini"}))
	store.Apply(NewEvent(sessionID, EventInterviewHelpStarted, "test", InterviewStartedData{GenerationID: "help-1", Trigger: "button", Question: "Tell me about yourself."}))
	store.Apply(NewEvent(sessionID, EventInterviewAutoDelta, "test", InterviewDeltaData{GenerationID: "auto-1", Delta: "Auto answer"}))
	store.Apply(NewEvent(sessionID, EventInterviewHelpDelta, "test", InterviewDeltaData{GenerationID: "help-1", Delta: "Help answer"}))

	state := store.Apply(NewEvent(sessionID, EventInterviewAutoDone, "test", InterviewDoneData{GenerationID: "auto-1", Question: "Tell me about yourself.", Text: "Auto answer done", Provider: "openrouter", Model: "gemini"}))
	if state.Interview.Auto.Text != "Auto answer done" || state.Interview.Auto.Streaming {
		t.Fatalf("auto lane = %#v", state.Interview.Auto)
	}
	if state.Interview.Help.Text != "Help answer" || !state.Interview.Help.Streaming {
		t.Fatalf("help lane should keep streaming independently: %#v", state.Interview.Help)
	}

	state = store.Apply(NewEvent(sessionID, EventInterviewHelpDone, "test", InterviewDoneData{GenerationID: "help-1", Question: "Tell me about yourself.", Text: "Help answer done", Provider: "openrouter", Model: "gemini"}))
	if state.Interview.Help.Text != "Help answer done" || state.Interview.Help.Streaming {
		t.Fatalf("help lane = %#v", state.Interview.Help)
	}
	if state.Interview.Auto.Text != "Auto answer done" {
		t.Fatalf("help lane overwrote auto answer: %#v", state.Interview)
	}
	state = store.Apply(NewEvent(sessionID, EventError, "test", ErrorData{Where: "interview.help", Message: "gemini timeout"}))
	if state.Interview.Help.Status != "error" || state.Interview.Help.Error != "gemini timeout" || state.Interview.Auto.Text != "Auto answer done" {
		t.Fatalf("lane error should remain isolated: %#v", state.Interview)
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

	store.Apply(NewEvent(sessionID, EventStudentAnswerRequest, "test", StudentAnswerRequestData{Trigger: "button"}))
	store.Apply(NewEvent(sessionID, EventStudentAnswerStarted, "test", StudentAnswerStartedData{GenerationID: "stu-1", Trigger: "button"}))
	store.Apply(NewEvent(sessionID, EventStudentAnswerDelta, "test", StudentAnswerDeltaData{GenerationID: "stu-1", Delta: "Это вопрос"}))
	state = store.Apply(NewEvent(sessionID, EventStudentAnswerDone, "test", StudentAnswerDoneData{GenerationID: "stu-1", Text: "Это вопрос о событии.", Model: "gemini-3.5-flash"}))
	if state.Student.AnswerStreaming || state.Student.AnswerText != "Это вопрос о событии." || state.Student.AnswerModel != "gemini-3.5-flash" {
		t.Fatalf("student answer = %#v", state.Student)
	}
	if len(state.Student.AnswerItems) != 2 {
		t.Fatalf("student answer history length = %d, want 2: %#v", len(state.Student.AnswerItems), state.Student.AnswerItems)
	}
	if state.Student.AnswerItems[0].Role != "user" || state.Student.AnswerItems[0].Text != "Помоги по последнему фрагменту" {
		t.Fatalf("student help request bubble = %#v", state.Student.AnswerItems[0])
	}
	if state.Student.AnswerItems[1].Role != "assistant" || state.Student.AnswerItems[1].Text != "Это вопрос о событии." || state.Student.AnswerItems[1].Streaming {
		t.Fatalf("student answer bubble = %#v", state.Student.AnswerItems[1])
	}

	state = store.Apply(NewEvent(sessionID, EventStudentAnswerTranslateStarted, "test", StudentAnswerTranslateStartedData{
		GenerationID: "stu-1",
		Direction:    StudentDirectionRuEn,
	}))
	if !state.Student.AnswerItems[1].TranslationStreaming || state.Student.AnswerItems[1].TranslationDirection != StudentDirectionRuEn {
		t.Fatalf("student answer translation should start on assistant bubble: %#v", state.Student.AnswerItems[1])
	}
	state = store.Apply(NewEvent(sessionID, EventStudentAnswerTranslateDone, "test", StudentAnswerTranslateDoneData{
		GenerationID: "stu-1",
		Text:         "This is a question about the event.",
		Direction:    StudentDirectionRuEn,
		Provider:     "cerebras",
		Model:        "gpt-oss-120b",
	}))
	if state.Student.AnswerItems[1].TranslationStreaming || state.Student.AnswerItems[1].TranslationText != "This is a question about the event." {
		t.Fatalf("student answer translation should finish on assistant bubble: %#v", state.Student.AnswerItems[1])
	}
}

func TestStoreStudentDirectionDoesNotRewindFromOldTranslation(t *testing.T) {
	store := NewStore()
	sessionID := "sess-student-direction"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventStudentDirection, "test", StudentDirectionData{Direction: StudentDirectionEnRu}))
	state := store.Apply(NewEvent(sessionID, EventStudentDirection, "test", StudentDirectionData{Direction: StudentDirectionRuEn}))
	if state.Student.Direction != StudentDirectionRuEn {
		t.Fatalf("direction before old translation = %q", state.Student.Direction)
	}

	state = store.Apply(NewEvent(sessionID, EventStudentTranslateDone, "test", StudentTranslateDoneData{
		GenerationID:  "trn-old",
		SourceEventID: "src-old",
		SourceText:    "Where are you?",
		Text:          "Где ты?",
		Direction:     StudentDirectionEnRu,
		Provider:      "cerebras",
		Model:         "gpt-oss-120b",
	}))

	if state.Student.Direction != StudentDirectionRuEn {
		t.Fatalf("old translation rewound direction to %q", state.Student.Direction)
	}
	if len(state.Student.Translations) != 1 || state.Student.Translations[0].Direction != StudentDirectionEnRu {
		t.Fatalf("translation item should keep its own direction: %#v", state.Student.Translations)
	}
}

func TestStoreApplyStudentOriginalFinalReplacesEquivalentPartial(t *testing.T) {
	store := NewStore()
	sessionID := "sess-student-stt"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	store.Apply(NewEvent(sessionID, EventSTTPartial, "test", SpeechData{
		Role:      "student_original",
		Text:      "А где твои вещи, Моко?",
		Source:    "student-system-audio",
		Speaker:   "unknown",
		SegmentID: "0000-000-unknown",
		Direction: StudentDirectionRuEn,
		Language:  "ru",
	}))
	state := store.Apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:      "student_original",
		Text:      "А где твои вещи, Моко?",
		Source:    "student-system-audio",
		Speaker:   "unknown",
		SegmentID: "0001-000-unknown",
		Direction: StudentDirectionRuEn,
		Language:  "ru",
	}))

	if len(state.Student.Originals) != 1 || !state.Student.Originals[0].Final {
		t.Fatalf("student final should replace equivalent partial: %#v", state.Student.Originals)
	}
	if len(state.Transcript) != 1 || !state.Transcript[0].Final {
		t.Fatalf("transcript final should replace equivalent partial: %#v", state.Transcript)
	}
}

func TestStoreApplyStudentSelfIsSeparateFromOriginals(t *testing.T) {
	store := NewStore()
	sessionID := "sess-student-self"
	store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))

	state := store.Apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:      "student_self",
		Text:      "Здравствуйте, меня зовут Кирилл.",
		Source:    CaptureSourceStudentMic,
		SegmentID: "mic-turn-1",
		Direction: StudentDirectionEnRu,
		Language:  "ru",
	}))

	if len(state.Student.Self) != 1 || state.Student.Self[0].Text != "Здравствуйте, меня зовут Кирилл." {
		t.Fatalf("student self transcript = %#v", state.Student.Self)
	}
	if len(state.Student.Originals) != 0 {
		t.Fatalf("student self should not enter originals: %#v", state.Student.Originals)
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
