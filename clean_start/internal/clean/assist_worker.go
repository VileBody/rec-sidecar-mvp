package clean

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

type AssistWorker struct {
	cfg       Config
	nc        *nats.Conn
	llm       *LLMClient
	logger    *slog.Logger
	memory    *memoryBook
	sub       *nats.Subscription
	mu        sync.Mutex
	cancels   map[string]context.CancelFunc
	activeGen map[string]string
}

func NewAssistWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *AssistWorker {
	return &AssistWorker{
		cfg:       cfg,
		nc:        nc,
		llm:       llm,
		logger:    logger.With("component", "assist-worker"),
		memory:    newMemoryBook(),
		cancels:   make(map[string]context.CancelFunc),
		activeGen: make(map[string]string),
	}
}

func (w *AssistWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectWildcard(w.cfg.SubjectPrefix), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		mem := w.memory.apply(event)
		if event.Type != EventAssistRequest {
			return
		}
		data, _ := DecodeData[AssistRequestData](event)
		w.startAssist(ctx, event.SessionID, mem, data.Trigger, data.Text)
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("assist worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *AssistWorker) Shutdown(context.Context) error {
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

func (w *AssistWorker) startAssist(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) {
	w.mu.Lock()
	if cancel := w.cancels[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("assist")
	w.cancels[sessionID] = cancel
	w.activeGen[sessionID] = generationID
	w.mu.Unlock()
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistStarted, "assist-worker", AssistStartedData{GenerationID: generationID, Trigger: trigger}))

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
			contextText += "\n--- Ручной запрос продавца ---\n" + text + "\n"
		}

		started := time.Now()
		fastText, fastModel, fallback, err := w.llm.HelpOpener(ctx, sessionID, contextText)
		if ctx.Err() != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistCanceled, "assist-worker", AssistStartedData{GenerationID: generationID, Trigger: trigger}))
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "assist-worker", ErrorData{Where: "assist.fast", Message: err.Error()}))
			return
		}
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistFastDone, "assist-worker", AssistFastDoneData{
			GenerationID: generationID,
			Text:         fastText,
			Model:        fastModel,
			Fallback:     fallback,
		}))

		var slowText string
		var slowModel string
		slowText, slowModel, err = w.llm.StreamHelpConstructive(ctx, sessionID, contextText, func(delta string) error {
			return PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistDelta, "assist-worker", AssistDeltaData{GenerationID: generationID, Delta: delta}))
		})
		if ctx.Err() != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistCanceled, "assist-worker", AssistStartedData{GenerationID: generationID, Trigger: trigger}))
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "assist-worker", ErrorData{Where: "assist.slow", Message: err.Error()}))
			return
		}
		w.logger.Info("assist generation done", "session_id", sessionID, "generation_id", generationID, "elapsed_ms", time.Since(started).Milliseconds(), "fast_model", fastModel, "slow_model", slowModel)
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventAssistDone, "assist-worker", AssistDoneData{
			GenerationID: generationID,
			FastText:     fastText,
			SlowText:     slowText,
			FastModel:    fastModel,
			SlowModel:    slowModel,
		}))
	}()
}
