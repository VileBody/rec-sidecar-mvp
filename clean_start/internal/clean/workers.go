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

type sessionMemory struct {
	Messages        []Message
	ClientPartial   string
	CurrentStage    string
	LastSellerInput string
	LastTriggerText string
}

func (m *sessionMemory) contextBlock() string {
	var b strings.Builder
	b.WriteString("Живой high-check B2C sales разговор.\n\n--- Диалог ---\n")
	if len(m.Messages) == 0 && m.ClientPartial == "" {
		b.WriteString("(диалог пока не начался)\n")
	}
	for _, msg := range m.Messages {
		role := "Client"
		if msg.Role == "seller" {
			role = "Seller"
		}
		b.WriteString(role)
		b.WriteString(": ")
		b.WriteString(msg.Text)
		b.WriteString("\n")
	}
	if m.ClientPartial != "" {
		b.WriteString("Client partial: ")
		b.WriteString(m.ClientPartial)
		b.WriteString("\n")
	}
	if m.CurrentStage != "" {
		b.WriteString("\n--- Current stage ---\n")
		b.WriteString(m.CurrentStage)
		b.WriteString("\n")
	}
	return b.String()
}

type memoryBook struct {
	mu       sync.Mutex
	sessions map[string]*sessionMemory
}

func newMemoryBook() *memoryBook {
	return &memoryBook{sessions: make(map[string]*sessionMemory)}
}

func (b *memoryBook) apply(event Event) *sessionMemory {
	b.mu.Lock()
	defer b.mu.Unlock()
	mem := b.sessions[event.SessionID]
	if mem == nil {
		mem = &sessionMemory{CurrentStage: "S2.1"}
		b.sessions[event.SessionID] = mem
	}
	switch event.Type {
	case EventSellerInput:
		if data, err := DecodeData[TextData](event); err == nil && data.Text != "" {
			mem.Messages = append(mem.Messages, Message{Role: "seller", Text: data.Text, CreatedAt: event.CreatedAt})
			mem.LastSellerInput = data.Text
		}
	case EventClientPartial:
		if data, err := DecodeData[TextData](event); err == nil {
			mem.ClientPartial = data.Text
		}
	case EventClientFinal:
		if data, err := DecodeData[TextData](event); err == nil && data.Text != "" {
			mem.Messages = append(mem.Messages, Message{Role: "client", Text: data.Text, CreatedAt: event.CreatedAt})
			mem.ClientPartial = ""
		}
	case EventStageCandidate, EventStageCommitted:
		if data, err := DecodeData[StageData](event); err == nil && data.Stage != "" {
			mem.CurrentStage = data.Stage
		}
	}
	copy := *mem
	copy.Messages = append([]Message(nil), mem.Messages...)
	return &copy
}

type SellerWorker struct {
	cfg       Config
	nc        *nats.Conn
	llm       *LLMClient
	logger    *slog.Logger
	memory    *memoryBook
	sub       *nats.Subscription
	mu        sync.Mutex
	cancels   map[string]context.CancelFunc
	activeGen map[string]string
	lastTexts map[string]string
}

func NewSellerWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *SellerWorker {
	return &SellerWorker{
		cfg:       cfg,
		nc:        nc,
		llm:       llm,
		logger:    logger.With("component", "seller-worker"),
		memory:    newMemoryBook(),
		cancels:   make(map[string]context.CancelFunc),
		activeGen: make(map[string]string),
		lastTexts: make(map[string]string),
	}
}

func (w *SellerWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(w.cfg.SubjectPrefix+".*.>", func(msg *nats.Msg) {
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
	return nil
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
	w.mu.Unlock()
	w.startGeneration(ctx, sessionID, mem, "diarized_partial", cleanText)
}

func (w *SellerWorker) startGeneration(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) {
	w.mu.Lock()
	if cancel := w.cancels[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("gen")
	w.cancels[sessionID] = cancel
	w.activeGen[sessionID] = generationID
	w.mu.Unlock()
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventSellerStarted, "seller-worker", SellerStartedData{GenerationID: generationID, Trigger: trigger}))

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
		question := "Дай одну следующую реплику продавца на русском: коротко, конкретно, без markdown, одно предложение."
		started := time.Now()
		var full string
		full, provider, model, err := w.llm.StreamSeller(ctx, sessionID, contextText, question, func(delta string) error {
			return PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventSellerDelta, "seller-worker", SellerDeltaData{GenerationID: generationID, Delta: delta}))
		})
		if ctx.Err() != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventSellerCanceled, "seller-worker", SellerStartedData{GenerationID: generationID, Trigger: trigger}))
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "seller-worker", ErrorData{Where: "seller", Message: err.Error()}))
			return
		}
		w.logger.Info("seller generation done", "session_id", sessionID, "generation_id", generationID, "elapsed_ms", time.Since(started).Milliseconds(), "provider", provider, "model", model)
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventSellerDone, "seller-worker", SellerDoneData{GenerationID: generationID, Text: full, Provider: provider, Model: model}))
	}()
}

type StageWorker struct {
	cfg    Config
	nc     *nats.Conn
	llm    *LLMClient
	logger *slog.Logger
	memory *memoryBook
	sub    *nats.Subscription
}

func NewStageWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *StageWorker {
	return &StageWorker{cfg: cfg, nc: nc, llm: llm, logger: logger.With("component", "stage-worker"), memory: newMemoryBook()}
}

func (w *StageWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(w.cfg.SubjectPrefix+".*.>", func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		mem := w.memory.apply(event)
		switch event.Type {
		case EventClientPartial:
			data, _ := DecodeData[TextData](event)
			if len([]rune(strings.TrimSpace(data.Text))) >= w.cfg.MinStageChars {
				go w.detect(ctx, event.SessionID, mem, EventStageCandidate)
			}
		case EventClientFinal:
			go w.detect(ctx, event.SessionID, mem, EventStageCommitted)
		}
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("stage worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *StageWorker) Shutdown(context.Context) error {
	if w.sub != nil {
		_ = w.sub.Unsubscribe()
	}
	return nil
}

func (w *StageWorker) detect(ctx context.Context, sessionID string, mem *sessionMemory, eventType string) {
	started := time.Now()
	stage, err := w.llm.DetectStage(ctx, sessionID, mem.contextBlock(), mem.CurrentStage)
	if err != nil {
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "stage-worker", ErrorData{Where: "stage", Message: err.Error()}))
		return
	}
	if stage == nil {
		return
	}
	w.logger.Info("stage detected", "session_id", sessionID, "stage", stage.Stage, "event", eventType, "elapsed_ms", time.Since(started).Milliseconds())
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, eventType, "stage-worker", stage))
}

type ScorecardWorker struct {
	cfg    Config
	nc     *nats.Conn
	logger *slog.Logger
	sub    *nats.Subscription
}

func NewScorecardWorker(cfg Config, nc *nats.Conn, logger *slog.Logger) *ScorecardWorker {
	return &ScorecardWorker{cfg: cfg, nc: nc, logger: logger.With("component", "scorecard-worker")}
}

func (w *ScorecardWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(w.cfg.SubjectPrefix+".*."+EventStageCommitted, func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		stage, err := DecodeData[StageData](event)
		if err != nil {
			return
		}
		scorecard := scorecardFromStage(stage)
		_ = PublishEvent(w.nc, w.cfg, NewEvent(event.SessionID, EventScorecardUpdate, "scorecard-worker", scorecard))
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("scorecard worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *ScorecardWorker) Shutdown(context.Context) error {
	if w.sub != nil {
		_ = w.sub.Unsubscribe()
	}
	return nil
}

func scorecardFromStage(stage StageData) ScorecardData {
	if len(stage.Scorecard) > 0 && string(stage.Scorecard) != "null" {
		return ScorecardData{
			Readiness:      "pending",
			ReadinessLabel: "Из LLM scorecard",
			ReadyToAdvance: false,
			NextAction:     stage.Step,
			Summary:        "Scorecard пришел вместе со stage response.",
			Source:         "llm-helper",
			Raw:            stage.Scorecard,
		}
	}
	return ScorecardData{
		Readiness:      "yellow",
		ReadinessLabel: "Нужно добрать факты",
		ReadyToAdvance: false,
		NextAction:     stage.Step,
		Summary:        "Первичная оценка: stage определен, но метрики считаются отдельной петлей.",
		Source:         "heuristic",
	}
}
