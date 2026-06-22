package clean

import "testing"

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
