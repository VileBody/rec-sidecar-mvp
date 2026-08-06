package clean

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

func TestInterviewWorkerAutoAndHelpCompleteIndependently(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/interview/question":
			writeJSON(w, http.StatusOK, interviewQuestionResponse{
				IsQuestion: true,
				Question:   "Tell me about your Bondora project.",
				Provider:   "openrouter",
				Model:      "google/gemini-3.5-flash",
			})
		case "/v1/interview/answer/stream":
			var req struct {
				Trigger string `json:"trigger"`
			}
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Fatal(err)
			}
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = w.Write([]byte("data: {\"event\":\"model\",\"provider\":\"openrouter\",\"model\":\"google/gemini-3.5-flash\"}\n\n"))
			_, _ = w.Write([]byte("data: {\"event\":\"delta\",\"text\":\"" + req.Trigger + " answer\"}\n\n"))
			_, _ = w.Write([]byte("data: {\"event\":\"done\"}\n\n"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	cfg := Config{LLMServiceURL: server.URL, LLMTimeout: 2 * time.Second, SubjectPrefix: "interview-worker-test"}
	worker := NewInterviewWorker(cfg, nil, NewLLMClient(cfg, noopLogger()), noopLogger())
	var mu sync.Mutex
	var events []Event
	worker.publish = func(_ *nats.Conn, _ Config, event Event) error {
		mu.Lock()
		events = append(events, event)
		mu.Unlock()
		return nil
	}

	mem := &sessionMemory{Messages: []Message{{Role: "client", Text: "Tell me about your Bondora project."}}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	worker.detectQuestion(ctx, "sess-interview", mem, "Tell me about your Bondora project.")
	worker.startHelp(ctx, "sess-interview", mem, "button", "")

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		autoDone := false
		helpDone := false
		canceled := false
		for _, event := range events {
			autoDone = autoDone || event.Type == EventInterviewAutoDone
			helpDone = helpDone || event.Type == EventInterviewHelpDone
			canceled = canceled || event.Type == EventInterviewAutoCanceled || event.Type == EventInterviewHelpCanceled
		}
		mu.Unlock()
		if autoDone && helpDone {
			if canceled {
				t.Fatal("one interview lane canceled the other")
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	t.Fatalf("both lanes did not finish: %#v", events)
}
