package clean

import (
	"context"
	"encoding/json"
	"log/slog"
	"strconv"
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
	revision                 int64
	generationSeq            int64
	inflight                 bool
	activeGenerationID       string
	activeBaseRevision       int64
	activeBaseText           string
	latestRevision           int64
	latestText               string
	latestPivotCheckRevision int64
	pendingReplan            bool
	pendingReplanLevel       string
	pendingRevision          int64
	pendingText              string
	pendingTrigger           string
	pendingMem               *sessionMemory
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
			w.maybeStartFromClientText(ctx, event.SessionID, mem, data.Text, "partial")
		case EventClientFinal:
			data, _ := DecodeData[TextData](event)
			w.maybeStartFromClientText(ctx, event.SessionID, mem, data.Text, "final")
		case EventSTTPartial:
			data, _ := DecodeData[SpeechData](event)
			if data.Role == "client" {
				w.maybeStartFromClientText(ctx, event.SessionID, mem, data.Text, "partial")
			}
		case EventSTTFinal:
			data, _ := DecodeData[SpeechData](event)
			if data.Role == "client" {
				w.maybeStartFromClientText(ctx, event.SessionID, mem, data.Text, "final")
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
	w.maybeStartFromClientText(ctx, sessionID, mem, text, "partial")
}

func (w *SellerWorker) maybeStartFromClientText(ctx context.Context, sessionID string, mem *sessionMemory, text, finality string) {
	cleanText := strings.TrimSpace(text)
	if cleanText == "" {
		return
	}
	isFinal := strings.EqualFold(strings.TrimSpace(finality), "final")
	if !isFinal && len([]rune(cleanText)) < w.cfg.MinSellerChars {
		return
	}
	w.mu.Lock()
	prev := w.lastTexts[sessionID]
	if !isFinal && len([]rune(cleanText))-len([]rune(prev)) < w.minSellerGrowth() && !endsSellerSentence(cleanText) {
		w.mu.Unlock()
		return
	}
	w.lastTexts[sessionID] = cleanText
	state := w.autoStateLocked(sessionID)
	state.revision++
	revision := state.revision
	state.latestRevision = revision
	state.latestText = cleanText
	generationSeq := state.generationSeq
	activeGenerationID := state.activeGenerationID
	activeBaseRevision := state.activeBaseRevision
	activeBaseText := state.activeBaseText
	generationActive := state.inflight
	currentDraft := strings.TrimSpace(mem.SellerDraft)
	w.mu.Unlock()

	if generationActive {
		w.startPivotGate(ctx, sessionID, mem, "zai_pivot_gate:"+finality, cleanText, currentDraft, revision, generationSeq, activeGenerationID, activeBaseRevision, activeBaseText)
		return
	}
	w.startReadyGate(ctx, sessionID, mem, "zai_ready_gate:"+finality, cleanText, currentDraft, revision, generationSeq, activeGenerationID)
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
		w.queueAutoReplanLocked(state, mem, trigger, text, "hard", true)
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
	state.activeBaseRevision = state.latestRevision
	state.activeBaseText = strings.TrimSpace(text)
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

func (w *SellerWorker) startReadyGate(parent context.Context, sessionID string, mem *sessionMemory, trigger, text, currentDraft string, revision, expectedGenerationSeq int64, expectedGenerationID string) {
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
			contextText += "\n--- Gate mode ---\nready_gate\n\n--- Client revision ---\n" + formatInt64(revision) + "\n\n--- Latest client text ---\n" + text + "\n"
			contextText += "\n--- Text finality ---\n" + gateTriggerFinality(trigger) + "\n"
		}
		started := time.Now()
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "ready_gate", Status: "sent", Trigger: trigger, GenerationID: gateID, Detail: "ZAI решает, пора ли запускать Gemini"})
		result, err := w.llm.ReadySellerGate(ctx, sessionID, contextText, currentDraft, revision)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "ready_gate", Status: "error", Trigger: trigger, GenerationID: gateID, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
			_ = w.publish(NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller.gate", Message: err.Error()}))
			return
		}
		elapsedMS := time.Since(started).Milliseconds()
		if result.ClientRevision != revision || !w.gateStillCurrent(sessionID, revision, expectedGenerationSeq, expectedGenerationID) {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "ready_gate", Status: "skipped", Trigger: trigger, GenerationID: gateID, Provider: result.Provider, Model: result.Model, Action: sellerActionOrDefault(result.Action, "stale"), ElapsedMS: elapsedMS, Detail: "устаревший ready gate result отброшен"})
			return
		}
		shouldGenerate := readyGateShouldGenerate(result, currentDraft)
		status := "received"
		detail := "gate запросил Gemini generation"
		if !shouldGenerate {
			status = "skipped"
			detail = "ready gate решил подождать или оставить текущую реплику"
		}
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "ready_gate", Status: status, Trigger: trigger, GenerationID: gateID, Provider: result.Provider, Model: result.Model, Action: result.Action, ElapsedMS: elapsedMS, Detail: detail})
		w.logger.Info("seller ready gate done", "session_id", sessionID, "action", result.Action, "confidence", result.Confidence, "elapsed_ms", elapsedMS, "provider", result.Provider, "model", result.Model, "current_chars", len([]rune(currentDraft)), "trigger_chars", len([]rune(text)))
		if !shouldGenerate {
			return
		}
		w.startGeneration(parent, sessionID, mem, trigger, text)
	}()
}

func (w *SellerWorker) startPivotGate(parent context.Context, sessionID string, mem *sessionMemory, trigger, text, currentDraft string, revision, expectedGenerationSeq int64, expectedGenerationID string, activeBaseRevision int64, activeBaseText string) {
	ctx, cancel := context.WithCancel(parent)
	pivotID := NewID("pivot")
	w.mu.Lock()
	w.gateCancels[pivotID] = cancel
	w.mu.Unlock()

	go func() {
		defer func() {
			w.mu.Lock()
			delete(w.gateCancels, pivotID)
			w.mu.Unlock()
		}()

		contextText := mem.contextBlock()
		contextText += "\n--- Gate mode ---\npivot_gate\n"
		contextText += "\n--- Client revision ---\n" + formatInt64(revision) + "\n"
		contextText += "\n--- Active Gemini generation ---\nGeneration ID: " + expectedGenerationID + "\nBase client revision: " + formatInt64(activeBaseRevision) + "\n"
		contextText += "\n--- Base client text used to start Gemini ---\n" + strings.TrimSpace(activeBaseText) + "\n"
		contextText += "\n--- Latest client text ---\n" + text + "\n"
		contextText += "\n--- Text finality ---\n" + gateTriggerFinality(trigger) + "\n"
		contextText += "\n--- Existing pending replan state ---\n" + w.pendingReplanStateString(sessionID) + "\n"
		started := time.Now()
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "pivot_gate", Status: "sent", Trigger: trigger, GenerationID: pivotID, Detail: "ZAI проверяет hard semantic pivot, пока Gemini генерирует"})
		result, err := w.llm.PivotSellerGate(ctx, sessionID, contextText, currentDraft, expectedGenerationID, activeBaseText, w.pendingReplanStateString(sessionID), revision)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "pivot_gate", Status: "error", Trigger: trigger, GenerationID: pivotID, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
			_ = w.publish(NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller.pivot_gate", Message: err.Error()}))
			return
		}
		elapsedMS := time.Since(started).Milliseconds()
		w.mu.Lock()
		state := w.autoStateLocked(sessionID)
		stale := result.ClientRevision != revision || state.generationSeq != expectedGenerationSeq || state.activeGenerationID != expectedGenerationID || !state.inflight || revision < state.latestPivotCheckRevision
		if !stale {
			state.latestPivotCheckRevision = revision
			switch result.Status {
			case "CHANGE_HARD":
				w.queueAutoReplanLocked(state, mem, trigger, text, "hard", false)
			case "NO_CHANGE":
				state.pendingReplan = false
				state.pendingRevision = revision
				state.pendingText = ""
				state.pendingTrigger = ""
				state.pendingMem = nil
				state.pendingReplanLevel = "none"
			case "ADAPT_SOFT":
				state.pendingRevision = revision
				state.pendingReplanLevel = "soft"
				if w.cfg.AutoReplanOnSoft {
					w.queueAutoReplanLocked(state, mem, trigger, text, "soft", false)
				}
			case "WAIT_NOISE":
				// Intentionally no state change. Noise must not clear an older hard pivot.
			}
		}
		w.mu.Unlock()
		if stale {
			w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "pivot_gate", Status: "skipped", Trigger: trigger, GenerationID: pivotID, Provider: result.Provider, Model: result.Model, Action: sellerActionOrDefault(result.Status, "stale"), ElapsedMS: elapsedMS, Detail: "устаревший pivot gate result отброшен"})
			return
		}
		status := pivotPipelineStatus(result.Status)
		detail := pivotPipelineDetail(result.Status)
		w.publishPipelineStatus(sessionID, PipelineStatusData{Component: "pivot_gate", Status: status, Trigger: trigger, GenerationID: pivotID, Provider: result.Provider, Model: result.Model, Action: result.Status, ElapsedMS: elapsedMS, Detail: detail})
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
		state.activeBaseRevision = 0
		state.activeBaseText = ""
		state.pendingReplan = false
		state.pendingReplanLevel = "none"
		state.pendingText = ""
		state.pendingTrigger = ""
		state.pendingMem = nil
		return nil
	}

	pendingMem := state.pendingMem
	pendingTrigger := state.pendingTrigger
	pendingText := state.pendingText
	state.pendingReplan = false
	state.pendingReplanLevel = "none"
	state.pendingText = ""
	state.pendingTrigger = ""
	state.pendingMem = nil
	next := w.prepareAutoGenerationLocked(run.parent, run.sessionID, pendingMem, pendingTrigger, pendingText)
	return &next
}

func (w *SellerWorker) queueAutoReplanLocked(state *sellerAutoState, mem *sessionMemory, trigger, text, level string, bumpRevision bool) {
	if bumpRevision {
		state.revision++
		state.latestRevision = state.revision
		state.latestText = strings.TrimSpace(text)
	}
	state.pendingReplan = true
	state.pendingReplanLevel = normalizedReplanLevel(level)
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

func (w *SellerWorker) pendingReplanStateString(sessionID string) string {
	w.mu.Lock()
	defer w.mu.Unlock()
	state := w.autoStateLocked(sessionID)
	if !state.pendingReplan && state.pendingReplanLevel == "" {
		return "pending_replan=false; level=none"
	}
	return "pending_replan=" + strconv.FormatBool(state.pendingReplan) +
		"; level=" + normalizedReplanLevel(state.pendingReplanLevel) +
		"; pending_revision=" + formatInt64(state.pendingRevision)
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

func readyGateShouldGenerate(result readyGateResponse, currentDraft string) bool {
	if result.Action != "GENERATE" {
		return false
	}
	threshold := 0.62
	if strings.TrimSpace(currentDraft) == "" {
		threshold = 0.55
	}
	if result.Confidence <= 0 {
		return true
	}
	return result.Confidence >= threshold
}

func normalizedReplanLevel(level string) string {
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "hard":
		return "hard"
	case "soft":
		return "soft"
	default:
		return "none"
	}
}

func pivotPipelineStatus(status string) string {
	switch status {
	case "CHANGE_HARD":
		return "received"
	case "ADAPT_SOFT", "NO_CHANGE", "WAIT_NOISE":
		return "skipped"
	default:
		return "skipped"
	}
}

func pivotPipelineDetail(status string) string {
	switch status {
	case "CHANGE_HARD":
		return "hard semantic pivot; поставили latest context в очередь"
	case "ADAPT_SOFT":
		return "soft context drift; логируем без автоперегенерации"
	case "NO_CHANGE":
		return "newer pivot check cleared pending replan"
	case "WAIT_NOISE":
		return "noise/filler; pending replan state unchanged"
	default:
		return "pivot gate returned unknown status"
	}
}

func formatInt64(value int64) string {
	return strconv.FormatInt(value, 10)
}

func gateTriggerFinality(trigger string) string {
	trigger = strings.ToLower(strings.TrimSpace(trigger))
	if strings.HasSuffix(trigger, ":final") {
		return "final"
	}
	if strings.HasSuffix(trigger, ":partial") {
		return "partial"
	}
	return "unknown"
}

func sellerActionOrDefault(action, fallback string) string {
	action = strings.TrimSpace(action)
	if action == "" {
		return fallback
	}
	return action
}
