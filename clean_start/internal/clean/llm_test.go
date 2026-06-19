package clean

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
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
