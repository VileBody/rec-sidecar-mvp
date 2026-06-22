package clean

import (
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
