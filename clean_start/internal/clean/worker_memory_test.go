package clean

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestStudentMemoryDirectionDoesNotRewindFromSTTOrTranslation(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-memory-direction"
	book.apply(NewEvent(sessionID, EventStudentDirection, "test", StudentDirectionData{Direction: StudentDirectionRuEn}))

	mem := book.apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:      "student_original",
		Text:      "Where are you?",
		Direction: StudentDirectionEnRu,
	}))
	if mem.StudentDirection != StudentDirectionRuEn {
		t.Fatalf("stt event rewound direction to %q", mem.StudentDirection)
	}

	mem = book.apply(NewEvent(sessionID, EventStudentTranslateDone, "test", StudentTranslateDoneData{
		GenerationID:  "trn-old",
		SourceEventID: "src-old",
		SourceText:    "Where are you?",
		Text:          "Где ты?",
		Direction:     StudentDirectionEnRu,
		Provider:      "cerebras",
		Model:         "gpt-oss-120b",
	}))
	if mem.StudentDirection != StudentDirectionRuEn {
		t.Fatalf("translation event rewound direction to %q", mem.StudentDirection)
	}
}

func TestEffectiveStudentDirectionPrefersSessionDirection(t *testing.T) {
	mem := &sessionMemory{StudentDirection: StudentDirectionRuEn}
	if got := effectiveStudentDirection("", mem); got != StudentDirectionRuEn {
		t.Fatalf("direction = %q, want %q", got, StudentDirectionRuEn)
	}
	if got := effectiveStudentDirection(StudentDirectionEnRu, mem); got != StudentDirectionEnRu {
		t.Fatalf("explicit direction = %q, want %q", got, StudentDirectionEnRu)
	}
}

func TestStudentMemoryIncludesHelpHistoryInContext(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-memory-help"
	book.apply(NewEvent(sessionID, EventStudentAnswerRequest, "test", StudentAnswerRequestData{Trigger: "button"}))
	mem := book.apply(NewEvent(sessionID, EventStudentAnswerDone, "test", StudentAnswerDoneData{
		GenerationID: "stu-1",
		Text:         "TL;DR: GIL ограничивает выполнение Python-кода.",
		Model:        "gemini-3.5-flash",
	}))

	context := mem.studentContextBlock()
	if !strings.Contains(context, "--- Help/chat history ---") {
		t.Fatalf("context missing help history: %s", context)
	}
	if !strings.Contains(context, "User: Помоги по последнему фрагменту") {
		t.Fatalf("context missing help request: %s", context)
	}
	if !strings.Contains(context, "Assistant: TL;DR: GIL ограничивает выполнение Python-кода.") {
		t.Fatalf("context missing previous answer: %s", context)
	}
}

func TestStudentHelpContextUsesOnlyInterlocutorOriginals(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-student-audio-split"
	book.apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:   "student_self",
		Source: CaptureSourceStudentMic,
		Text:   "Let me ask a follow-up question.",
	}))
	mem := book.apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:   "student_original",
		Source: CaptureSourceStudentSystemAudio,
		Text:   "Can you explain the difference between TCP and UDP?",
	}))

	context := mem.studentContextBlock()
	if strings.Contains(context, "Let me ask a follow-up question") {
		t.Fatalf("student self mic leaked into help context:\n%s", context)
	}
	if !strings.Contains(context, "Can you explain the difference between TCP and UDP?") {
		t.Fatalf("interlocutor system audio missing from help context:\n%s", context)
	}
}

func TestSalesMemoryIncludesStageAgendaAndScorecardInContext(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-sales-guidance"
	book.apply(NewEvent(sessionID, EventStageCommitted, "test", StageData{
		Stage:   "S2.2",
		Title:   "Квалификация: текущая ситуация",
		Agenda:  "узнать текущую ситуацию, боли и ограничения",
		Emotion: "признать нагрузку клиента",
		Step:    "добрать факты текущей ситуации",
	}))
	mem := book.apply(NewEvent(sessionID, EventScorecardUpdate, "test", ScorecardData{
		Readiness:      "yellow",
		ReadinessLabel: "Почти",
		ReadyToAdvance: false,
		Summary:        "Не хватает конкретики по ограничениям.",
		NextAction:     "Уточнить: Где именно сейчас упираетесь — деньги, время или доверие к формату?",
		Source:         "llm-helper",
		Raw:            json.RawMessage(`{"checks":[{"id":"current_context","result":"hit"},{"id":"pain","result":"pending"}]}`),
	}))

	context := mem.contextBlock()
	for _, want := range []string{
		"--- Current stage / agenda ---",
		"Stage: S2.2",
		"Agenda: узнать текущую ситуацию, боли и ограничения",
		"Canonical next step: добрать факты текущей ситуации",
		"--- Current scorecard ---",
		"Readiness: yellow",
		"Recommended next action: Уточнить: Где именно сейчас упираетесь",
		`"id":"pain"`,
	} {
		if !strings.Contains(context, want) {
			t.Fatalf("context missing %q:\n%s", want, context)
		}
	}
}
