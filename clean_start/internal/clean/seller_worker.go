package clean

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

type SellerWorker struct {
	cfg          Config
	nc           *nats.Conn
	llm          *LLMClient
	logger       *slog.Logger
	memory       *memoryBook
	sub          *nats.Subscription
	publishEvent func(*nats.Conn, Config, Event) error
	mu           sync.Mutex
	cancels      map[string]context.CancelFunc
	gateCancels  map[string]context.CancelFunc
	activeGen    map[string]string
	autoStates   map[string]*sellerAutoState
	lastStages   map[string]string
	lastTexts    map[string]string
}

type sellerAutoState struct {
	revision            int64
	generationSeq       int64
	inflight            bool
	activeGenerationID  string
	pendingReplan       bool
	pendingFromValidity bool
	pendingRevision     int64
	pendingText         string
	pendingTrigger      string
	pendingMem          *sessionMemory
}

type sellerGenerationRun struct {
	parent       context.Context
	ctx          context.Context
	sessionID    string
	mem          *sessionMemory
	trigger      string
	text         string
	component    string
	generationID string
	manual       bool
	sentDetail   string
}

func NewSellerWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *SellerWorker {
	return &SellerWorker{
		cfg:          cfg,
		nc:           nc,
		llm:          llm,
		logger:       logger.With("component", "seller-worker"),
		memory:       newMemoryBook(),
		publishEvent: PublishEvent,
		cancels:      make(map[string]context.CancelFunc),
		gateCancels:  make(map[string]context.CancelFunc),
		activeGen:    make(map[string]string),
		autoStates:   make(map[string]*sellerAutoState),
		lastStages:   make(map[string]string),
		lastTexts:    make(map[string]string),
	}
}

func (w *SellerWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectWildcard(w.cfg.SubjectPrefix), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			w.logger.Warn("bad event", "error", err)
			return
		}
		mem := w.memory.apply(event)
		switch event.Type {
		case EventSellerRequest:
			data, _ := DecodeData[SellerRequestData](event)
			w.startGeneration(ctx, event.SessionID, mem, data.Trigger, data.Text)
		case EventClientPartial:
			data, _ := DecodeData[TextData](event)
			w.maybeStartFromPartial(ctx, event.SessionID, mem, data.Text)
		case EventClientFinal:
			data, _ := DecodeData[TextData](event)
			w.startGeneration(ctx, event.SessionID, mem, "client_final", data.Text)
		case EventSTTPartial:
			data, _ := DecodeData[SpeechData](event)
			if data.Role == "client" {
				w.maybeStartFromPartial(ctx, event.SessionID, mem, data.Text)
			}
		case EventSTTFinal:
			data, _ := DecodeData[SpeechData](event)
			if data.Role == "client" {
				w.startGeneration(ctx, event.SessionID, mem, "stt_client_final", data.Text)
			}
		case EventStageCandidate, EventStageCommitted:
			data, _ := DecodeData[StageData](event)
			w.maybeStartFromStage(ctx, event.SessionID, mem, event.Type, data.Stage)
		}
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("seller worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *SellerWorker) Shutdown(context.Context) error {
	if w.sub != nil {
		_ = w.sub.Unsubscribe()
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, cancel := range w.cancels {
		cancel()
	}
	for _, cancel := range w.gateCancels {
		cancel()
	}
	return nil
}

func (w *SellerWorker) publish(event Event) error {
	w.memory.apply(event)
	return w.publishEvent(w.nc, w.cfg, event)
}

func (w *SellerWorker) publishPipelineStatus(sessionID string, data PipelineStatusData) {
	if data.Component == "" || data.Status == "" {
		return
	}
	_ = w.publish(NewEvent(sessionID, EventPipelineStatus, "seller-worker", data))
}

func (w *SellerWorker) maybeStartFromPartial(ctx context.Context, sessionID string, mem *sessionMemory, text string) {
	cleanText := strings.TrimSpace(text)
	if len([]rune(cleanText)) < w.cfg.MinSellerChars {
		return
	}
	w.mu.Lock()
	prev := w.lastTexts[sessionID]
	if len([]rune(cleanText))-len([]rune(prev)) < w.minSellerGrowth() && !endsSellerSentence(cleanText) {
		w.mu.Unlock()
		return
	}
	w.lastTexts[sessionID] = cleanText
	state := w.autoStateLocked(sessionID)
	state.revision++
	revision := state.revision
	generationSeq := state.generationSeq
	activeGenerationID := state.activeGenerationID
	generationActive := state.inflight
	currentDraft := strings.TrimSpace(mem.SellerDraft)
	w.mu.Unlock()

	if generationActive {
		w.startValidity(ctx, sessionID, mem, "zai_semantic_validity", cleanText, currentDraft, revision, generationSeq, activeGenerationID)
		return
	}
	if currentDraft == "" {
		w.startGeneration(ctx, sessionID, mem, "no_current_reply", cleanText)
		return
	}
	w.startGate(ctx, sessionID, mem, "zai_semantic_gate", cleanText, currentDraft, revision, generationSeq, activeGenerationID)
}

func (w *SellerWorker) maybeStartFromStage(ctx context.Context, sessionID string, mem *sessionMemory, trigger, stage string) {
	stage = strings.TrimSpace(stage)
	if stage == "" {
		return
	}
	w.mu.Lock()
	previous := w.lastStages[sessionID]
	if previous == "" {
		previous = "S2.1"
	}
	w.lastStages[sessionID] = stage
	w.mu.Unlock()
	if stage == previous {
		return
	}
	w.startGeneration(ctx, sessionID, mem, "stage_changed:"+trigger, "stage="+stage)
}

func (w *SellerWorker) startGeneration(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) {
	manual := isManualSellerTrigger(trigger)
	if manual {
		run := w.prepareManualGeneration(parent, sessionID, mem, trigger, text)
		go w.runGeneration(run)
		return
	}
	w.mu.Lock()
	state := w.autoStateLocked(sessionID)
	if state.inflight {
		w.queueAutoReplanLocked(state, mem, trigger, text, false, true)
		w.mu.Unlock()
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "seller_reply", Status: "skipped", Trigger: trigger, Detail: "Gemini уже генерирует реплику, поставили обновление в очередь"})
		return
	}
	run := w.prepareAutoGenerationLocked(parent, sessionID, mem, trigger, text)
	w.mu.Unlock()
	go w.runGeneration(run)
}

func (w *SellerWorker) prepareManualGeneration(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) sellerGenerationRun {
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("gen")
	generationKey := sellerGenerationKey(sessionID, "manual_reply")
	w.mu.Lock()
	w.cancels[generationKey] = cancel
	w.activeGen[generationKey] = generationID
	w.mu.Unlock()
	return sellerGenerationRun{
		parent:       parent,
		ctx:          ctx,
		sessionID:    sessionID,
		mem:          cloneSessionMemory(mem),
		trigger:      trigger,
		text:         text,
		component:    "manual_reply",
		generationID: generationID,
		manual:       true,
		sentDetail:   "отправили прямой запрос в Gemini без ZAI gate",
	}
}

func (w *SellerWorker) prepareAutoGenerationLocked(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) sellerGenerationRun {
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("gen")
	generationKey := sellerGenerationKey(sessionID, "seller_reply")
	state := w.autoStateLocked(sessionID)
	state.generationSeq++
	state.inflight = true
	state.activeGenerationID = generationID
	w.cancels[generationKey] = cancel
	w.activeGen[generationKey] = generationID
	return sellerGenerationRun{
		parent:       parent,
		ctx:          ctx,
		sessionID:    sessionID,
		mem:          cloneSessionMemory(mem),
		trigger:      trigger,
		text:         text,
		component:    "seller_reply",
		generationID: generationID,
		sentDetail:   "отправили запрос на новую реплику",
	}
}

func (w *SellerWorker) runGeneration(run sellerGenerationRun) {
	defer func() {
		if run.manual {
			w.finishManualGeneration(run)
			return
		}
		if next := w.finishAutoGeneration(run); next != nil {
			go w.runGeneration(*next)
		}
	}()

	contextText := run.mem.contextBlock()
	if run.text != "" {
		contextText += "\n--- Триггер ---\n" + run.text + "\n"
	}
	currentDraft := strings.TrimSpace(run.mem.SellerDraft)
	if run.manual {
		currentDraft = ""
	}
	started := time.Now()
	w.publishPipelineStatus(run.sessionID, PipelineStatusData{Component: run.component, Status: "sent", Trigger: run.trigger, GenerationID: run.generationID, Detail: run.sentDetail})
	suggestion, err := w.llm.LiveSellerSuggestion(run.ctx, run.sessionID, contextText, currentDraft, true)
	if run.ctx.Err() != nil {
		return
	}
	if err != nil {
		w.publishPipelineStatus(run.sessionID, PipelineStatusData{Component: run.component, Status: "error", Trigger: run.trigger, GenerationID: run.generationID, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
		_ = w.publish(NewEvent(run.sessionID, EventError, "seller-worker", ErrorData{Where: "seller", Message: err.Error()}))
		return
	}
	if suggestion.Action == "skip" {
		w.publishPipelineStatus(run.sessionID, PipelineStatusData{Component: run.component, Status: "skipped", Trigger: run.trigger, GenerationID: run.generationID, Provider: suggestion.Provider, Model: suggestion.Model, Action: suggestion.Action, ElapsedMS: time.Since(started).Milliseconds(), Detail: "LLM решила оставить текущую реплику"})
		w.logger.Info("seller force generation skipped", "session_id", run.sessionID, "generation_id", run.generationID, "elapsed_ms", time.Since(started).Milliseconds(), "provider", suggestion.Provider, "model", suggestion.Model)
		return
	}
	if suggestion.Text == "" {
		w.publishPipelineStatus(run.sessionID, PipelineStatusData{Component: run.component, Status: "error", Trigger: run.trigger, GenerationID: run.generationID, Provider: suggestion.Provider, Model: suggestion.Model, Action: suggestion.Action, ElapsedMS: time.Since(started).Milliseconds(), Detail: "empty seller suggestion"})
		_ = w.publish(NewEvent(run.sessionID, EventError, "seller-worker", ErrorData{Where: "seller", Message: "empty seller suggestion"}))
		return
	}
	_ = w.publish(NewEvent(run.sessionID, EventSellerStarted, "seller-worker", SellerStartedData{GenerationID: run.generationID, Trigger: run.trigger}))
	_ = w.publish(NewEvent(run.sessionID, EventSellerDelta, "seller-worker", SellerDeltaData{GenerationID: run.generationID, Delta: suggestion.Text}))
	w.logger.Info("seller generation done", "session_id", run.sessionID, "generation_id", run.generationID, "trigger", run.trigger, "elapsed_ms", time.Since(started).Milliseconds(), "provider", suggestion.Provider, "model", suggestion.Model)
	w.publishPipelineStatus(run.sessionID, PipelineStatusData{Component: run.component, Status: "received", Trigger: run.trigger, GenerationID: run.generationID, Provider: suggestion.Provider, Model: suggestion.Model, Action: suggestion.Action, ElapsedMS: time.Since(started).Milliseconds(), Detail: "новая реплика готова"})
	_ = w.publish(NewEvent(run.sessionID, EventSellerDone, "seller-worker", SellerDoneData{GenerationID: run.generationID, Text: suggestion.Text, Provider: suggestion.Provider, Model: suggestion.Model}))
}

func sellerGenerationKey(sessionID, component string) string {
	return sessionID + "/" + component
}

func (w *SellerWorker) startGate(parent context.Context, sessionID string, mem *sessionMemory, trigger, text, currentDraft string, revision, expectedGenerationSeq int64, expectedGenerationID string) {
	ctx, cancel := context.WithCancel(parent)
	gateID := NewID("gate")
	w.mu.Lock()
	w.gateCancels[gateID] = cancel
	w.mu.Unlock()

	go func() {
		defer func() {
			w.mu.Lock()
			delete(w.gateCancels, gateID)
			w.mu.Unlock()
		}()

		contextText := mem.contextBlock()
		if text != "" {
			contextText += "\n--- Триггер ---\n" + text + "\n"
		}
		started := time.Now()
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_gate", Status: "sent", Trigger: trigger, GenerationID: gateID, Detail: "проверяем, пора ли менять реплику"})
		suggestion, err := w.llm.LiveSellerSuggestion(ctx, sessionID, contextText, currentDraft, false)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_gate", Status: "error", Trigger: trigger, GenerationID: gateID, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
			_ = w.publish(NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller.gate", Message: err.Error()}))
			return
		}
		elapsedMS := time.Since(started).Milliseconds()
		if !w.gateStillCurrent(sessionID, revision, expectedGenerationSeq, expectedGenerationID) {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_gate", Status: "skipped", Trigger: trigger, GenerationID: gateID, Provider: suggestion.Provider, Model: suggestion.Model, Action: sellerActionOrDefault(suggestion.Action, "stale"), ElapsedMS: elapsedMS, Detail: "устаревший gate result отброшен"})
			return
		}
		shouldGenerate := sellerActionNeedsGeneration(suggestion.Action)
		status := "received"
		detail := "gate запросил Gemini generation"
		if !shouldGenerate {
			status = "skipped"
			detail = "gate решил подождать или оставить текущую реплику"
		}
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_gate", Status: status, Trigger: trigger, GenerationID: gateID, Provider: suggestion.Provider, Model: suggestion.Model, Action: suggestion.Action, ElapsedMS: elapsedMS, Detail: detail})
		w.logger.Info("seller gate done", "session_id", sessionID, "action", suggestion.Action, "elapsed_ms", elapsedMS, "provider", suggestion.Provider, "model", suggestion.Model, "current_chars", len([]rune(currentDraft)), "trigger_chars", len([]rune(text)))
		if !shouldGenerate {
			return
		}
		w.startGeneration(parent, sessionID, mem, trigger, text)
	}()
}

func (w *SellerWorker) startValidity(parent context.Context, sessionID string, mem *sessionMemory, trigger, text, currentDraft string, revision, expectedGenerationSeq int64, expectedGenerationID string) {
	ctx, cancel := context.WithCancel(parent)
	validityID := NewID("validity")
	w.mu.Lock()
	w.gateCancels[validityID] = cancel
	w.mu.Unlock()

	go func() {
		defer func() {
			w.mu.Lock()
			delete(w.gateCancels, validityID)
			w.mu.Unlock()
		}()

		contextText := mem.contextBlock()
		if text != "" {
			contextText += "\n--- Latest partial ---\n" + text + "\n"
		}
		if expectedGenerationID != "" {
			contextText += "\n--- Active Gemini generation ---\n" + expectedGenerationID + "\n"
		}
		started := time.Now()
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_validity", Status: "sent", Trigger: trigger, GenerationID: validityID, Detail: "проверяем, актуальна ли активная Gemini для latest partial"})
		suggestion, err := w.llm.LiveSellerSuggestion(ctx, sessionID, contextText, currentDraft, false)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_validity", Status: "error", Trigger: trigger, GenerationID: validityID, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
			_ = w.publish(NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller.validity", Message: err.Error()}))
			return
		}
		elapsedMS := time.Since(started).Milliseconds()
		invalidated := sellerActionNeedsGeneration(suggestion.Action)
		w.mu.Lock()
		state := w.autoStateLocked(sessionID)
		stale := state.revision != revision || state.generationSeq != expectedGenerationSeq || state.activeGenerationID != expectedGenerationID || !state.inflight
		if !stale {
			if invalidated {
				w.queueAutoReplanLocked(state, mem, trigger, text, true, false)
			} else if state.pendingFromValidity {
				state.pendingReplan = false
				state.pendingFromValidity = false
				state.pendingRevision = revision
				state.pendingText = ""
				state.pendingTrigger = ""
				state.pendingMem = nil
			}
		}
		w.mu.Unlock()
		if stale {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_validity", Status: "skipped", Trigger: trigger, GenerationID: validityID, Provider: suggestion.Provider, Model: suggestion.Model, Action: sellerActionOrDefault(suggestion.Action, "stale"), ElapsedMS: elapsedMS, Detail: "устаревший validity result отброшен"})
			return
		}
		status := "received"
		detail := "active Gemini invalidated; поставили latest partial в очередь"
		if !invalidated {
			status = "skipped"
			detail = "active Gemini остается актуальной для latest partial"
			if strings.EqualFold(strings.TrimSpace(suggestion.Action), "wait") {
				detail = "validity попросила подождать больше контекста"
			}
		}
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "zai_validity", Status: status, Trigger: trigger, GenerationID: validityID, Provider: suggestion.Provider, Model: suggestion.Model, Action: suggestion.Action, ElapsedMS: elapsedMS, Detail: detail})
	}()
}

func (w *SellerWorker) finishManualGeneration(run sellerGenerationRun) {
	generationKey := sellerGenerationKey(run.sessionID, run.component)
	w.mu.Lock()
	if w.activeGen[generationKey] == run.generationID {
		delete(w.cancels, generationKey)
		delete(w.activeGen, generationKey)
	}
	w.mu.Unlock()
}

func (w *SellerWorker) finishAutoGeneration(run sellerGenerationRun) *sellerGenerationRun {
	generationKey := sellerGenerationKey(run.sessionID, "seller_reply")
	w.mu.Lock()
	defer w.mu.Unlock()

	state := w.autoStateLocked(run.sessionID)
	if state.activeGenerationID != run.generationID {
		return nil
	}
	delete(w.cancels, generationKey)
	delete(w.activeGen, generationKey)

	if run.parent.Err() != nil || !state.pendingReplan {
		state.inflight = false
		state.activeGenerationID = ""
		state.pendingReplan = false
		state.pendingFromValidity = false
		state.pendingText = ""
		state.pendingTrigger = ""
		state.pendingMem = nil
		return nil
	}

	pendingMem := state.pendingMem
	pendingTrigger := state.pendingTrigger
	pendingText := state.pendingText
	state.pendingReplan = false
	state.pendingFromValidity = false
	state.pendingText = ""
	state.pendingTrigger = ""
	state.pendingMem = nil
	next := w.prepareAutoGenerationLocked(run.parent, run.sessionID, pendingMem, pendingTrigger, pendingText)
	return &next
}

func (w *SellerWorker) queueAutoReplanLocked(state *sellerAutoState, mem *sessionMemory, trigger, text string, fromValidity, bumpRevision bool) {
	if bumpRevision {
		state.revision++
	}
	state.pendingReplan = true
	state.pendingFromValidity = fromValidity
	state.pendingRevision = state.revision
	state.pendingText = text
	state.pendingTrigger = trigger
	state.pendingMem = cloneSessionMemory(mem)
}

func (w *SellerWorker) autoStateLocked(sessionID string) *sellerAutoState {
	state := w.autoStates[sessionID]
	if state == nil {
		state = &sellerAutoState{}
		w.autoStates[sessionID] = state
	}
	return state
}

func (w *SellerWorker) gateStillCurrent(sessionID string, revision, expectedGenerationSeq int64, expectedGenerationID string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	state := w.autoStateLocked(sessionID)
	return state.revision == revision && state.generationSeq == expectedGenerationSeq && state.activeGenerationID == expectedGenerationID
}

func (w *SellerWorker) minSellerGrowth() int {
	if w.cfg.MinSellerGrowth >= 16 {
		return w.cfg.MinSellerGrowth
	}
	return 16
}

func endsSellerSentence(text string) bool {
	return strings.HasSuffix(text, ".") || strings.HasSuffix(text, "?") || strings.HasSuffix(text, "!")
}

func sellerActionNeedsGeneration(action string) bool {
	switch strings.ToLower(strings.TrimSpace(action)) {
	case "generate", "suggest", "invalidated", "on":
		return true
	default:
		return false
	}
}

func sellerActionOrDefault(action, fallback string) string {
	action = strings.TrimSpace(action)
	if action == "" {
		return fallback
	}
	return action
}
