package clean

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

type sellerLiveCall struct {
	force       bool
	content     string
	currentText string
	respond     chan liveSellerResponse
}

type sellerWorkerHarness struct {
	worker *SellerWorker
	server *httptest.Server
	calls  chan *sellerLiveCall

	mu         sync.Mutex
	events     []Event
	forceCalls int
}

func newSellerWorkerHarness(t *testing.T) *sellerWorkerHarness {
	t.Helper()
	h := &sellerWorkerHarness{calls: make(chan *sellerLiveCall, 32)}
	h.server = httptest.NewServer(http.HandlerFunc(h.handleLive))
	cfg := Config{
		LLMServiceURL:   h.server.URL,
		LLMTimeout:      2 * time.Second,
		MinSellerChars:  1,
		MinSellerGrowth: 16,
		SubjectPrefix:   "seller-worker-test",
	}
	h.worker = NewSellerWorker(cfg, nil, NewLLMClient(cfg, noopLogger()), noopLogger())
	h.worker.publishEvent = func(_ *nats.Conn, _ Config, event Event) error {
		h.mu.Lock()
		h.events = append(h.events, event)
		h.mu.Unlock()
		return nil
	}
	return h
}

func (h *sellerWorkerHarness) close() {
	h.server.Close()
}

func (h *sellerWorkerHarness) handleLive(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/v1/coach/live" {
		http.NotFound(w, r)
		return
	}
	var req struct {
		Content     string `json:"content"`
		CurrentText string `json:"current_text"`
		Force       bool   `json:"force"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	call := &sellerLiveCall{
		force:       req.Force,
		content:     req.Content,
		currentText: req.CurrentText,
		respond:     make(chan liveSellerResponse, 1),
	}
	h.mu.Lock()
	if call.force {
		h.forceCalls++
	}
	h.mu.Unlock()

	select {
	case h.calls <- call:
	case <-r.Context().Done():
		return
	}
	select {
	case response := <-call.respond:
		writeJSON(w, http.StatusOK, response)
	case <-r.Context().Done():
		return
	}
}

func (h *sellerWorkerHarness) nextCall(t *testing.T) *sellerLiveCall {
	t.Helper()
	select {
	case call := <-h.calls:
		return call
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for LLM call")
		return nil
	}
}

func (h *sellerWorkerHarness) forceCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.forceCalls
}

func (h *sellerWorkerHarness) snapshotEvents() []Event {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]Event(nil), h.events...)
}

func (h *sellerWorkerHarness) hasPipelineStatus(component, status, action, detailContains string) bool {
	for _, event := range h.snapshotEvents() {
		if event.Type != EventPipelineStatus {
			continue
		}
		data, err := DecodeData[PipelineStatusData](event)
		if err != nil {
			continue
		}
		if data.Component != component || data.Status != status {
			continue
		}
		if action != "" && data.Action != action {
			continue
		}
		if detailContains != "" && !strings.Contains(data.Detail, detailContains) {
			continue
		}
		return true
	}
	return false
}

func waitForSellerWorker(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("timed out waiting for seller worker condition")
}

func sellerTestMem(draft string) *sessionMemory {
	return &sessionMemory{CurrentStage: "S2.1", SellerDraft: draft}
}

func TestSellerWorkerStaleGateDiscard(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-stale-gate"
	mem := sellerTestMem("старый автоответ")
	firstPartial := strings.Repeat("а", 16)
	latestPartial := firstPartial + strings.Repeat("б", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, mem, firstPartial)
	firstGate := h.nextCall(t)
	if firstGate.force {
		t.Fatal("first partial should start ZAI gate, not Gemini")
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, mem, latestPartial)
	latestGate := h.nextCall(t)
	if latestGate.force {
		t.Fatal("latest partial should start ZAI gate, not Gemini")
	}

	firstGate.respond <- liveSellerResponse{Action: "generate", Provider: "zai", Model: "gate"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("zai_gate", "skipped", "generate", "устаревший")
	})
	if got := h.forceCount(); got != 0 {
		t.Fatalf("stale gate started Gemini: force calls = %d", got)
	}

	latestGate.respond <- liveSellerResponse{Action: "wait", Provider: "zai", Model: "gate"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("zai_gate", "skipped", "wait", "подождать")
	})
}

func TestSellerWorkerPendingReplanDuringGemini(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-pending"
	firstPartial := strings.Repeat("а", 16)
	latestPartial := firstPartial + strings.Repeat("б", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), firstPartial)
	firstGemini := h.nextCall(t)
	if !firstGemini.force {
		t.Fatal("first auto generation should call Gemini")
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.force {
		t.Fatal("partial during Gemini should call ZAI validity, not second Gemini")
	}
	validity.respond <- liveSellerResponse{Action: "invalidated", Provider: "zai", Model: "validity"}

	waitForSellerWorker(t, func() bool {
		h.worker.mu.Lock()
		defer h.worker.mu.Unlock()
		state := h.worker.autoStates[sessionID]
		return state != nil && state.pendingReplan && state.pendingFromValidity && state.pendingText == latestPartial
	})
	if got := h.forceCount(); got != 1 {
		t.Fatalf("Gemini should still have only one force call while pending, got %d", got)
	}

	cancel()
	_ = firstGemini
}

func TestSellerWorkerNoSecondGeminiWhileBusy(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-no-second"
	firstPartial := strings.Repeat("а", 16)
	latestPartial := firstPartial + strings.Repeat("б", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), firstPartial)
	firstGemini := h.nextCall(t)
	if !firstGemini.force {
		t.Fatal("first auto generation should call Gemini")
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.force {
		t.Fatal("busy seller reply should launch validity, not Gemini")
	}
	validity.respond <- liveSellerResponse{Action: "invalidated", Provider: "zai", Model: "validity"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("zai_validity", "received", "invalidated", "очередь")
	})

	time.Sleep(50 * time.Millisecond)
	if got := h.forceCount(); got != 1 {
		t.Fatalf("expected no second Gemini before first is done, force calls = %d", got)
	}

	firstGemini.respond <- liveSellerResponse{Action: "suggest", Text: "первый ответ", Provider: "vertex", Model: "gemini"}
	secondGemini := h.nextCall(t)
	if !secondGemini.force {
		t.Fatal("handoff after first Gemini should be another Gemini call")
	}
	secondGemini.respond <- liveSellerResponse{Action: "suggest", Text: "обновленный ответ", Provider: "vertex", Model: "gemini"}
	waitForSellerWorker(t, func() bool {
		return h.hasSellerDoneText("обновленный ответ")
	})
}

func TestSellerWorkerImmediateHandoffAfterGeminiDoneIfInvalidated(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-handoff"
	firstPartial := strings.Repeat("а", 16)
	latestPartial := firstPartial + strings.Repeat("б", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), firstPartial)
	firstGemini := h.nextCall(t)
	if !firstGemini.force {
		t.Fatal("first auto generation should call Gemini")
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.force {
		t.Fatal("partial during Gemini should call validity")
	}
	validity.respond <- liveSellerResponse{Action: "invalidated", Provider: "zai", Model: "validity"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("zai_validity", "received", "invalidated", "очередь")
	})

	firstGemini.respond <- liveSellerResponse{Action: "suggest", Text: "старый ответ", Provider: "vertex", Model: "gemini"}
	secondGemini := h.nextCall(t)
	if !secondGemini.force {
		t.Fatal("pending replan should immediately hand off to Gemini")
	}
	if got := h.forceCount(); got != 2 {
		t.Fatalf("force calls after handoff = %d, want 2", got)
	}
	waitForSellerWorker(t, func() bool {
		return h.hasSellerDoneText("старый ответ") && h.hasPipelineStatus("seller_reply", "sent", "", "новую реплику")
	})

	secondGemini.respond <- liveSellerResponse{Action: "suggest", Text: "обновленный ответ", Provider: "vertex", Model: "gemini"}
	waitForSellerWorker(t, func() bool {
		return h.hasSellerDoneText("обновленный ответ")
	})
}

func (h *sellerWorkerHarness) hasSellerDoneText(text string) bool {
	for _, event := range h.snapshotEvents() {
		if event.Type != EventSellerDone {
			continue
		}
		data, err := DecodeData[SellerDoneData](event)
		if err == nil && data.Text == text {
			return true
		}
	}
	return false
}
