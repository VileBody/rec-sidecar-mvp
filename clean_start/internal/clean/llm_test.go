package clean

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLiveSellerSuggestionCallsLiveEndpoint(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/coach/live" {
			t.Fatalf("path = %q, want /v1/coach/live", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"action":   "suggest",
			"text":     "Уточните, что именно раньше не сработало.",
			"provider": "vertex",
			"model":    "gemini-3.5-flash",
		})
	}))
	defer server.Close()

	client := NewLLMClient(Config{LLMServiceURL: server.URL}, slog.Default())
	out, err := client.LiveSellerSuggestion(context.Background(), "sess-test", "context", "old reply", true)
	if err != nil {
		t.Fatal(err)
	}
	if out.Action != "suggest" || out.Provider != "vertex" || out.Model != "gemini-3.5-flash" {
		t.Fatalf("unexpected response: %#v", out)
	}
	if got["run_id"] != "sess-test" || got["content"] != "context" || got["current_text"] != "old reply" || got["force"] != true {
		t.Fatalf("unexpected request body: %#v", got)
	}
}

func TestSessionMemoryTracksSellerDraftForGate(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-test"
	started := NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-1", Trigger: "test"})
	delta := NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-1", Delta: "Первая версия"})
	done := NewEvent(sessionID, EventSellerDone, "test", SellerDoneData{GenerationID: "gen-1", Text: "Финальная версия", Provider: "vertex", Model: "gemini"})

	book.apply(started)
	mem := book.apply(delta)
	if mem.SellerDraft != "Первая версия" {
		t.Fatalf("draft after delta = %q", mem.SellerDraft)
	}
	mem = book.apply(done)
	if mem.SellerDraft != "Финальная версия" {
		t.Fatalf("draft after done = %q", mem.SellerDraft)
	}
	if mem.SellerGenerationID != "" {
		t.Fatalf("seller generation id should be cleared, got %q", mem.SellerGenerationID)
	}
}

func TestSessionMemoryIgnoresDuplicateSellerEvents(t *testing.T) {
	book := newMemoryBook()
	sessionID := "sess-test"
	started := NewEvent(sessionID, EventSellerStarted, "test", SellerStartedData{GenerationID: "gen-1", Trigger: "test"})
	delta := NewEvent(sessionID, EventSellerDelta, "test", SellerDeltaData{GenerationID: "gen-1", Delta: "Версия"})

	book.apply(started)
	book.apply(delta)
	mem := book.apply(delta)

	if mem.SellerDraft != "Версия" {
		t.Fatalf("draft after duplicate delta = %q", mem.SellerDraft)
	}
}

func TestScanSSEHandlesFinalEventWithoutBlankLine(t *testing.T) {
	var got []streamEvent
	err := scanSSE(strings.NewReader("data: {\"event\":\"delta\",\"text\":\"раз\"}\n"), func(event streamEvent) error {
		got = append(got, event)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Event != "delta" || got[0].Text != "раз" {
		t.Fatalf("events = %#v", got)
	}
}

func TestScanSSEPropagatesHandlerError(t *testing.T) {
	wantErr := errors.New("stop")
	err := scanSSE(strings.NewReader("data: {\"event\":\"delta\",\"text\":\"раз\"}\n\n"), func(streamEvent) error {
		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("err = %v, want %v", err, wantErr)
	}
}

func TestScanSSECombinesMultilineData(t *testing.T) {
	input := "data: {\"event\":\"delta\",\ndata: \"text\":\"раз\"}\n\n"
	var got streamEvent
	err := scanSSE(strings.NewReader(input), func(event streamEvent) error {
		got = event
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.Event != "delta" || got.Text != "раз" {
		t.Fatalf("event = %#v", got)
	}
}

func TestDetectInterviewQuestionUsesDedicatedEndpoint(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/interview/question" {
			t.Fatalf("path = %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		writeJSON(w, http.StatusOK, interviewQuestionResponse{
			IsQuestion: true,
			Question:   "Tell me about yourself.",
			Provider:   "openrouter",
			Model:      "google/gemini-3.5-flash",
		})
	}))
	defer server.Close()

	client := NewLLMClient(Config{LLMServiceURL: server.URL}, slog.Default())
	out, err := client.DetectInterviewQuestion(context.Background(), "sess-interview", "Interviewer: Tell me about yourself", "Tell me about yourself")
	if err != nil {
		t.Fatal(err)
	}
	if !out.IsQuestion || out.Question != "Tell me about yourself." || out.Provider != "openrouter" {
		t.Fatalf("response = %#v", out)
	}
	if got["run_id"] != "sess-interview" || got["candidate"] != "Tell me about yourself" {
		t.Fatalf("request = %#v", got)
	}
}

func TestStreamInterviewAnswerKeepsTriggerAndProvider(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/interview/answer/stream" {
			t.Fatalf("path = %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: {\"event\":\"model\",\"provider\":\"openrouter\",\"model\":\"google/gemini-3.5-flash\"}\n\n"))
		_, _ = w.Write([]byte("data: {\"event\":\"delta\",\"text\":\"The most relevant \"}\n\n"))
		_, _ = w.Write([]byte("data: {\"event\":\"delta\",\"text\":\"example is Bondora.\"}\n\n"))
		_, _ = w.Write([]byte("data: {\"event\":\"done\"}\n\n"))
	}))
	defer server.Close()

	var deltas []string
	client := NewLLMClient(Config{LLMServiceURL: server.URL}, slog.Default())
	text, provider, model, err := client.StreamInterviewAnswer(context.Background(), "sess-interview", "context", "Tell me about Bondora", "help", func(delta string) error {
		deltas = append(deltas, delta)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if text != "The most relevant example is Bondora." || provider != "openrouter" || model != "google/gemini-3.5-flash" {
		t.Fatalf("stream result text=%q provider=%q model=%q", text, provider, model)
	}
	if got["trigger"] != "help" || got["question"] != "Tell me about Bondora" || len(deltas) != 2 {
		t.Fatalf("request=%#v deltas=%#v", got, deltas)
	}
}
