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
	cfg         Config
	nc          *nats.Conn
	llm         *LLMClient
	logger      *slog.Logger
	memory      *memoryBook
	sub         *nats.Subscription
	mu          sync.Mutex
	cancels     map[string]context.CancelFunc
	gateCancels map[string]context.CancelFunc
	activeGate  map[string]string
	activeGen   map[string]string
	lastStages  map[string]string
	lastTexts   map[string]string
}

func NewSellerWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *SellerWorker {
	return &SellerWorker{
		cfg:         cfg,
		nc:          nc,
		llm:         llm,
		logger:      logger.With("component", "seller-worker"),
		memory:      newMemoryBook(),
		cancels:     make(map[string]context.CancelFunc),
		gateCancels: make(map[string]context.CancelFunc),
		activeGate:  make(map[string]string),
		activeGen:   make(map[string]string),
		lastStages:  make(map[string]string),
		lastTexts:   make(map[string]string),
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
	return PublishEvent(w.nc, w.cfg, event)
}

func (w *SellerWorker) maybeStartFromPartial(ctx context.Context, sessionID string, mem *sessionMemory, text string) {
	cleanText := strings.TrimSpace(text)
	if len([]rune(cleanText)) < w.cfg.MinSellerChars {
		return
	}
	w.mu.Lock()
	prev := w.lastTexts[sessionID]
	if len([]rune(cleanText))-len([]rune(prev)) < w.cfg.MinSellerGrowth && !strings.HasSuffix(cleanText, ".") && !strings.HasSuffix(cleanText, "?") && !strings.HasSuffix(cleanText, "!") {
		w.mu.Unlock()
		return
	}
	w.lastTexts[sessionID] = cleanText
	currentDraft := strings.TrimSpace(mem.SellerDraft)
	_, generationActive := w.cancels[sessionID]
	w.mu.Unlock()

	if currentDraft == "" {
		if generationActive {
			return
		}
		w.startGeneration(ctx, sessionID, mem, "no_current_reply", cleanText)
		return
	}
	w.startGate(ctx, sessionID, mem, "zai_semantic_gate", cleanText, currentDraft)
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
	w.mu.Lock()
	if cancel := w.cancels[sessionID]; cancel != nil {
		cancel()
	}
	if cancel := w.gateCancels[sessionID]; cancel != nil {
		cancel()
		delete(w.gateCancels, sessionID)
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("gen")
	w.cancels[sessionID] = cancel
	w.activeGen[sessionID] = generationID
	w.mu.Unlock()

	go func() {
		defer func() {
			w.mu.Lock()
			if w.activeGen[sessionID] == generationID {
				delete(w.cancels, sessionID)
				delete(w.activeGen, sessionID)
			}
			w.mu.Unlock()
		}()

		contextText := mem.contextBlock()
		if text != "" {
			contextText += "\n--- Триггер ---\n" + text + "\n"
		}
		started := time.Now()
		suggestion, err := w.llm.LiveSellerSuggestion(ctx, sessionID, contextText, strings.TrimSpace(mem.SellerDraft), true)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller", Message: err.Error()}))
			return
		}
		if suggestion.Action == "skip" {
			w.logger.Info("seller force generation skipped", "session_id", sessionID, "generation_id", generationID, "elapsed_ms", time.Since(started).Milliseconds(), "provider", suggestion.Provider, "model", suggestion.Model)
			return
		}
		if suggestion.Text == "" {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller", Message: "empty seller suggestion"}))
			return
		}
		_ = w.publish(NewEvent(sessionID, EventSellerStarted, "seller-worker", SellerStartedData{GenerationID: generationID, Trigger: trigger}))
		_ = w.publish(NewEvent(sessionID, EventSellerDelta, "seller-worker", SellerDeltaData{GenerationID: generationID, Delta: suggestion.Text}))
		w.logger.Info("seller generation done", "session_id", sessionID, "generation_id", generationID, "trigger", trigger, "elapsed_ms", time.Since(started).Milliseconds(), "provider", suggestion.Provider, "model", suggestion.Model)
		_ = w.publish(NewEvent(sessionID, EventSellerDone, "seller-worker", SellerDoneData{GenerationID: generationID, Text: suggestion.Text, Provider: suggestion.Provider, Model: suggestion.Model}))
	}()
}

func (w *SellerWorker) startGate(parent context.Context, sessionID string, mem *sessionMemory, trigger, text, currentDraft string) {
	w.mu.Lock()
	if cancel := w.gateCancels[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	gateID := NewID("gate")
	w.gateCancels[sessionID] = cancel
	w.activeGate[sessionID] = gateID
	w.mu.Unlock()

	go func() {
		defer func() {
			w.mu.Lock()
			if w.activeGate[sessionID] == gateID {
				delete(w.gateCancels, sessionID)
				delete(w.activeGate, sessionID)
			}
			w.mu.Unlock()
		}()

		contextText := mem.contextBlock()
		if text != "" {
			contextText += "\n--- Триггер ---\n" + text + "\n"
		}
		started := time.Now()
		suggestion, err := w.llm.LiveSellerSuggestion(ctx, sessionID, contextText, currentDraft, false)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller.gate", Message: err.Error()}))
			return
		}
		w.logger.Info("seller gate done", "session_id", sessionID, "action", suggestion.Action, "elapsed_ms", time.Since(started).Milliseconds(), "provider", suggestion.Provider, "model", suggestion.Model, "current_chars", len([]rune(currentDraft)), "trigger_chars", len([]rune(text)))
		if suggestion.Action != "suggest" {
			return
		}
		if suggestion.Text == "" {
			w.startGeneration(parent, sessionID, mem, "zai_gate_empty_suggest", text)
			return
		}
		w.publishImmediateSuggestion(sessionID, trigger, suggestion)
	}()
}

func (w *SellerWorker) publishImmediateSuggestion(sessionID, trigger string, suggestion liveSellerResponse) {
	generationID := NewID("gen")
	w.mu.Lock()
	if cancel := w.cancels[sessionID]; cancel != nil {
		cancel()
	}
	delete(w.cancels, sessionID)
	w.activeGen[sessionID] = generationID
	w.mu.Unlock()

	_ = w.publish(NewEvent(sessionID, EventSellerStarted, "seller-worker", SellerStartedData{GenerationID: generationID, Trigger: trigger}))
	_ = w.publish(NewEvent(sessionID, EventSellerDelta, "seller-worker", SellerDeltaData{GenerationID: generationID, Delta: suggestion.Text}))
	_ = w.publish(NewEvent(sessionID, EventSellerDone, "seller-worker", SellerDoneData{GenerationID: generationID, Text: suggestion.Text, Provider: suggestion.Provider, Model: suggestion.Model}))

	w.mu.Lock()
	if w.activeGen[sessionID] == generationID {
		delete(w.cancels, sessionID)
		delete(w.activeGen, sessionID)
	}
	w.mu.Unlock()
}
