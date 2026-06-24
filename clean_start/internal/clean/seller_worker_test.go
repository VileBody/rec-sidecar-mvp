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
	kind        string
	force       bool
	content     string
	currentText string
	respond     chan any
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
	if r.URL.Path != "/v1/coach/live" && r.URL.Path != "/v1/coach/live/ready-gate" && r.URL.Path != "/v1/coach/live/pivot-gate" {
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
	kind := "generate"
	if strings.HasSuffix(r.URL.Path, "/ready-gate") {
		kind = "ready_gate"
	} else if strings.HasSuffix(r.URL.Path, "/pivot-gate") {
		kind = "pivot_gate"
	}
	call := &sellerLiveCall{
		kind:        kind,
		force:       req.Force,
		content:     req.Content,
		currentText: req.CurrentText,
		respond:     make(chan any, 1),
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
	if firstGate.kind != "ready_gate" || firstGate.force {
		t.Fatalf("first partial call = kind %q force %v, want ready gate", firstGate.kind, firstGate.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, mem, latestPartial)
	latestGate := h.nextCall(t)
	if latestGate.kind != "ready_gate" || latestGate.force {
		t.Fatalf("latest partial call = kind %q force %v, want ready gate", latestGate.kind, latestGate.force)
	}

	firstGate.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("ready_gate", "skipped", "GENERATE", "устаревший")
	})
	if got := h.forceCount(); got != 0 {
		t.Fatalf("stale gate started Gemini: force calls = %d", got)
	}

	latestGate.respond <- readyGateResponse{ClientRevision: 2, Action: "WAIT", Confidence: 1, Provider: "zai", Model: "gate"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("ready_gate", "skipped", "WAIT", "оставить")
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
	ready := h.nextCall(t)
	if ready.kind != "ready_gate" {
		t.Fatalf("first call kind = %q, want ready gate", ready.kind)
	}
	ready.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	firstGemini := h.nextCall(t)
	if firstGemini.kind != "generate" || !firstGemini.force {
		t.Fatalf("first auto generation call = kind %q force %v, want Gemini", firstGemini.kind, firstGemini.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.kind != "pivot_gate" || validity.force {
		t.Fatalf("partial during Gemini call = kind %q force %v, want pivot gate", validity.kind, validity.force)
	}
	validity.respond <- pivotGateResponse{ClientRevision: 2, Status: "CHANGE_HARD", Confidence: 1, Provider: "zai", Model: "pivot"}

	waitForSellerWorker(t, func() bool {
		h.worker.mu.Lock()
		defer h.worker.mu.Unlock()
		state := h.worker.autoStates[sessionID]
		return state != nil && state.pendingReplan && state.pendingReplanLevel == "hard" && state.pendingText == latestPartial
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
	ready := h.nextCall(t)
	if ready.kind != "ready_gate" {
		t.Fatalf("first call kind = %q, want ready gate", ready.kind)
	}
	ready.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	firstGemini := h.nextCall(t)
	if firstGemini.kind != "generate" || !firstGemini.force {
		t.Fatalf("first auto generation call = kind %q force %v, want Gemini", firstGemini.kind, firstGemini.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.kind != "pivot_gate" || validity.force {
		t.Fatalf("busy seller reply call = kind %q force %v, want pivot gate", validity.kind, validity.force)
	}
	validity.respond <- pivotGateResponse{ClientRevision: 2, Status: "CHANGE_HARD", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "received", "CHANGE_HARD", "очередь")
	})

	time.Sleep(50 * time.Millisecond)
	if got := h.forceCount(); got != 1 {
		t.Fatalf("expected no second Gemini before first is done, force calls = %d", got)
	}

	firstGemini.respond <- liveSellerResponse{Action: "suggest", Text: "первый ответ", Provider: "vertex", Model: "gemini"}
	secondGemini := h.nextCall(t)
	if secondGemini.kind != "generate" || !secondGemini.force {
		t.Fatalf("handoff call = kind %q force %v, want Gemini", secondGemini.kind, secondGemini.force)
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
	ready := h.nextCall(t)
	if ready.kind != "ready_gate" {
		t.Fatalf("first call kind = %q, want ready gate", ready.kind)
	}
	ready.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	firstGemini := h.nextCall(t)
	if firstGemini.kind != "generate" || !firstGemini.force {
		t.Fatalf("first auto generation call = kind %q force %v, want Gemini", firstGemini.kind, firstGemini.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), latestPartial)
	validity := h.nextCall(t)
	if validity.kind != "pivot_gate" || validity.force {
		t.Fatalf("partial during Gemini call = kind %q force %v, want pivot gate", validity.kind, validity.force)
	}
	validity.respond <- pivotGateResponse{ClientRevision: 2, Status: "CHANGE_HARD", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "received", "CHANGE_HARD", "очередь")
	})

	firstGemini.respond <- liveSellerResponse{Action: "suggest", Text: "старый ответ", Provider: "vertex", Model: "gemini"}
	secondGemini := h.nextCall(t)
	if secondGemini.kind != "generate" || !secondGemini.force {
		t.Fatalf("pending replan handoff call = kind %q force %v, want Gemini", secondGemini.kind, secondGemini.force)
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

func TestSellerWorkerNewestPivotWins(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-newest-pivot"
	firstPartial := strings.Repeat("а", 16)
	secondPartial := firstPartial + strings.Repeat("б", 16)
	thirdPartial := secondPartial + strings.Repeat("в", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), firstPartial)
	ready := h.nextCall(t)
	ready.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	firstGemini := h.nextCall(t)
	if firstGemini.kind != "generate" || !firstGemini.force {
		t.Fatalf("first auto generation call = kind %q force %v, want Gemini", firstGemini.kind, firstGemini.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), secondPartial)
	olderPivot := h.nextCall(t)
	if olderPivot.kind != "pivot_gate" {
		t.Fatalf("older pivot kind = %q, want pivot_gate", olderPivot.kind)
	}
	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), thirdPartial)
	newerPivot := h.nextCall(t)
	if newerPivot.kind != "pivot_gate" {
		t.Fatalf("newer pivot kind = %q, want pivot_gate", newerPivot.kind)
	}

	newerPivot.respond <- pivotGateResponse{ClientRevision: 3, Status: "NO_CHANGE", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "skipped", "NO_CHANGE", "cleared")
	})
	olderPivot.respond <- pivotGateResponse{ClientRevision: 2, Status: "CHANGE_HARD", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "skipped", "CHANGE_HARD", "устаревший")
	})

	h.worker.mu.Lock()
	state := h.worker.autoStates[sessionID]
	pending := state != nil && state.pendingReplan
	h.worker.mu.Unlock()
	if pending {
		t.Fatal("older hard pivot should not set pending replan after newer NO_CHANGE")
	}

	cancel()
	_ = firstGemini
}

func TestSellerWorkerWaitNoiseKeepsPendingReplan(t *testing.T) {
	h := newSellerWorkerHarness(t)
	defer h.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sessionID := "sess-noise-keeps-pending"
	firstPartial := strings.Repeat("а", 16)
	secondPartial := firstPartial + strings.Repeat("б", 16)
	thirdPartial := secondPartial + strings.Repeat("в", 16)

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), firstPartial)
	ready := h.nextCall(t)
	ready.respond <- readyGateResponse{ClientRevision: 1, Action: "GENERATE", Confidence: 1, Provider: "zai", Model: "gate"}
	firstGemini := h.nextCall(t)
	if firstGemini.kind != "generate" || !firstGemini.force {
		t.Fatalf("first auto generation call = kind %q force %v, want Gemini", firstGemini.kind, firstGemini.force)
	}

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), secondPartial)
	hardPivot := h.nextCall(t)
	hardPivot.respond <- pivotGateResponse{ClientRevision: 2, Status: "CHANGE_HARD", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "received", "CHANGE_HARD", "очередь")
	})

	h.worker.maybeStartFromPartial(ctx, sessionID, sellerTestMem(""), thirdPartial)
	noisePivot := h.nextCall(t)
	noisePivot.respond <- pivotGateResponse{ClientRevision: 3, Status: "WAIT_NOISE", Confidence: 1, Provider: "zai", Model: "pivot"}
	waitForSellerWorker(t, func() bool {
		return h.hasPipelineStatus("pivot_gate", "skipped", "WAIT_NOISE", "unchanged")
	})

	h.worker.mu.Lock()
	state := h.worker.autoStates[sessionID]
	pending := state != nil && state.pendingReplan && state.pendingReplanLevel == "hard" && state.pendingText == secondPartial
	h.worker.mu.Unlock()
	if !pending {
		t.Fatal("WAIT_NOISE should leave existing hard pending replan untouched")
	}

	cancel()
	_ = firstGemini
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
