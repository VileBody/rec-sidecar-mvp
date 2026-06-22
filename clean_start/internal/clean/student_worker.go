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

type StudentWorker struct {
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

func NewStudentWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *StudentWorker {
	return &StudentWorker{
		cfg:       cfg,
		nc:        nc,
		llm:       llm,
		logger:    logger.With("component", "student-worker"),
		memory:    newMemoryBook(),
		cancels:   make(map[string]context.CancelFunc),
		activeGen: make(map[string]string),
	}
}

func (w *StudentWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectWildcard(w.cfg.SubjectPrefix), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		mem := w.memory.apply(event)
		switch event.Type {
		case EventStudentInput:
			data, _ := DecodeData[StudentInputData](event)
			w.startTranslation(ctx, event.SessionID, event.ID, data.Text, effectiveStudentDirection(data.Direction, mem))
		case EventSTTFinal:
			data, _ := DecodeData[SpeechData](event)
			if data.Role == "student_original" {
				w.startTranslation(ctx, event.SessionID, event.ID, data.Text, effectiveStudentDirection("", mem))
			}
		case EventStudentAnswerRequest:
			data, _ := DecodeData[StudentAnswerRequestData](event)
			w.startAnswer(ctx, event.SessionID, mem, data.Trigger, data.Text)
		}
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("student worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *StudentWorker) Shutdown(context.Context) error {
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

func effectiveStudentDirection(eventDirection string, mem *sessionMemory) string {
	for _, direction := range []string{eventDirection, mem.StudentDirection, StudentDirectionEnRu} {
		if strings.TrimSpace(direction) == "" {
			continue
		}
		if normalized, err := normalizeStudentDirection(direction); err == nil {
			return normalized
		}
	}
	return StudentDirectionEnRu
}

func (w *StudentWorker) startTranslation(parent context.Context, sessionID, sourceEventID, text, direction string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	direction, err := normalizeStudentDirection(direction)
	if err != nil {
		direction = StudentDirectionEnRu
	}
	generationID := NewID("trn")
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentTranslateStarted, "student-worker", StudentTranslateStartedData{
		GenerationID:  generationID,
		SourceEventID: sourceEventID,
		Direction:     direction,
	}))
	go func() {
		started := time.Now()
		ctx, cancel := context.WithTimeout(parent, w.cfg.LLMTimeout)
		defer cancel()
		translated, provider, model, err := w.llm.StudentTranslate(ctx, sessionID, text, direction)
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "student-worker", ErrorData{Where: "student.translate", Message: err.Error()}))
			return
		}
		w.logger.Info("student translation done", "session_id", sessionID, "direction", direction, "provider", provider, "model", model, "elapsed_ms", time.Since(started).Milliseconds())
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentTranslateDone, "student-worker", StudentTranslateDoneData{
			GenerationID:  generationID,
			SourceEventID: sourceEventID,
			SourceText:    text,
			Text:          translated,
			Direction:     direction,
			Provider:      provider,
			Model:         model,
		}))
	}()
}

func (w *StudentWorker) startAnswer(parent context.Context, sessionID string, mem *sessionMemory, trigger, question string) {
	w.mu.Lock()
	if cancel := w.cancels[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("stu")
	w.cancels[sessionID] = cancel
	w.activeGen[sessionID] = generationID
	w.mu.Unlock()
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerStarted, "student-worker", StudentAnswerStartedData{GenerationID: generationID, Trigger: trigger}))

	go func() {
		defer func() {
			w.mu.Lock()
			if w.activeGen[sessionID] == generationID {
				delete(w.cancels, sessionID)
				delete(w.activeGen, sessionID)
			}
			w.mu.Unlock()
		}()
		started := time.Now()
		contextText := mem.studentContextBlock()
		answer, model, err := w.llm.StreamStudentAnswer(ctx, sessionID, contextText, strings.TrimSpace(question), func(delta string) error {
			return PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerDelta, "student-worker", StudentAnswerDeltaData{GenerationID: generationID, Delta: delta}))
		})
		if ctx.Err() != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerCanceled, "student-worker", StudentAnswerStartedData{GenerationID: generationID, Trigger: trigger}))
			return
		}
		if err != nil {
			_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventError, "student-worker", ErrorData{Where: "student.answer", Message: err.Error()}))
			return
		}
		w.logger.Info("student answer done", "session_id", sessionID, "generation_id", generationID, "model", model, "elapsed_ms", time.Since(started).Milliseconds())
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerDone, "student-worker", StudentAnswerDoneData{
			GenerationID: generationID,
			Text:         answer,
			Model:        model,
		}))
		w.startAnswerTranslation(parent, sessionID, generationID, answer)
	}()
}

func (w *StudentWorker) startAnswerTranslation(parent context.Context, sessionID, generationID, text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	direction := StudentDirectionRuEn
	_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerTranslateStarted, "student-worker", StudentAnswerTranslateStartedData{
		GenerationID: generationID,
		Direction:    direction,
	}))
	go func() {
		started := time.Now()
		ctx, cancel := context.WithTimeout(parent, w.cfg.LLMTimeout)
		defer cancel()
		translated, provider, model, err := w.llm.StudentTranslate(ctx, sessionID, text, direction)
		if err != nil {
			w.logger.Warn("student answer translation failed", "session_id", sessionID, "generation_id", generationID, "error", err)
			return
		}
		w.logger.Info("student answer translation done", "session_id", sessionID, "generation_id", generationID, "direction", direction, "provider", provider, "model", model, "elapsed_ms", time.Since(started).Milliseconds())
		_ = PublishEvent(w.nc, w.cfg, NewEvent(sessionID, EventStudentAnswerTranslateDone, "student-worker", StudentAnswerTranslateDoneData{
			GenerationID: generationID,
			Text:         translated,
			Direction:    direction,
			Provider:     provider,
			Model:        model,
		}))
	}()
}
