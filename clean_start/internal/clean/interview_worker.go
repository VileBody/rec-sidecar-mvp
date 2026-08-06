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

type InterviewWorker struct {
	cfg       Config
	nc        *nats.Conn
	llm       *LLMClient
	logger    *slog.Logger
	memory    *memoryBook
	sub       *nats.Subscription
	publish   func(*nats.Conn, Config, Event) error
	mu        sync.Mutex
	detectSeq map[string]int64
	detects   map[string]context.CancelFunc
	auto      map[string]context.CancelFunc
	help      map[string]context.CancelFunc
	autoGen   map[string]string
	helpGen   map[string]string
}

func NewInterviewWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *InterviewWorker {
	return &InterviewWorker{
		cfg:       cfg,
		nc:        nc,
		llm:       llm,
		logger:    logger.With("component", "interview-worker"),
		memory:    newMemoryBook(),
		publish:   PublishEvent,
		detectSeq: make(map[string]int64),
		detects:   make(map[string]context.CancelFunc),
		auto:      make(map[string]context.CancelFunc),
		help:      make(map[string]context.CancelFunc),
		autoGen:   make(map[string]string),
		helpGen:   make(map[string]string),
	}
}

func (w *InterviewWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectWildcard(w.cfg.SubjectPrefix), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			w.logger.Warn("bad event", "error", err)
			return
		}
		event = EventWithNATSHeaders(event, msg.Header)
		handleCtx, span := StartEventSpan(ctx, event, "interview_worker.handle_event")
		defer EndSpan(span, nil)
		mem := w.memory.apply(event)
		switch event.Type {
		case EventSTTFinal:
			data, _ := DecodeData[SpeechData](event)
			if data.AccountRole == UserRolePersonal && data.Role == "client" && strings.TrimSpace(data.Text) != "" {
				w.detectQuestion(handleCtx, event.SessionID, mem, data.Text)
			}
		case EventInterviewHelpRequest:
			data, _ := DecodeData[InterviewHelpRequestData](event)
			w.startHelp(handleCtx, event.SessionID, mem, data.Trigger, data.Text)
		}
	})
	if err != nil {
		return err
	}
	w.sub = sub
	w.logger.Info("interview worker subscribed")
	<-ctx.Done()
	return ctx.Err()
}

func (w *InterviewWorker) Shutdown(context.Context) error {
	if w.sub != nil {
		_ = w.sub.Unsubscribe()
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, cancels := range []map[string]context.CancelFunc{w.detects, w.auto, w.help} {
		for _, cancel := range cancels {
			cancel()
		}
	}
	return nil
}

func (w *InterviewWorker) emit(ctx context.Context, event Event) error {
	event = EventWithTraceContext(ctx, event)
	w.memory.apply(event)
	return w.publish(w.nc, w.cfg, event)
}

func (w *InterviewWorker) detectQuestion(parent context.Context, sessionID string, mem *sessionMemory, candidate string) {
	w.mu.Lock()
	if cancel := w.detects[sessionID]; cancel != nil {
		cancel()
	}
	w.detectSeq[sessionID]++
	sequence := w.detectSeq[sessionID]
	ctx, cancel := context.WithCancel(parent)
	w.detects[sessionID] = cancel
	w.mu.Unlock()

	contextText := mem.interviewContextBlock()
	go func() {
		identified, err := w.llm.DetectInterviewQuestion(ctx, sessionID, contextText, candidate)
		if ctx.Err() != nil {
			return
		}
		w.mu.Lock()
		current := w.detectSeq[sessionID] == sequence
		if current {
			delete(w.detects, sessionID)
		}
		w.mu.Unlock()
		if !current {
			return
		}
		if err != nil {
			w.logger.Warn("question detection failed", "session_id", sessionID, "error", err)
			_ = w.emit(ctx, NewEvent(sessionID, EventError, "interview-worker", ErrorData{Where: "interview.question", Message: err.Error()}))
			return
		}
		if !identified.IsQuestion {
			return
		}
		w.startAutoAnswer(parent, sessionID, mem, identified)
	}()
}

func (w *InterviewWorker) startAutoAnswer(parent context.Context, sessionID string, mem *sessionMemory, identified interviewQuestionResponse) {
	w.mu.Lock()
	if cancel := w.auto[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("interview-auto")
	w.auto[sessionID] = cancel
	w.autoGen[sessionID] = generationID
	w.mu.Unlock()

	started := NewEvent(sessionID, EventInterviewAutoStarted, "interview-worker", InterviewStartedData{
		GenerationID: generationID,
		Trigger:      "question",
		Question:     identified.Question,
	})
	_ = w.emit(ctx, started)
	_ = w.emit(ctx, NewEvent(sessionID, EventInterviewQuestionIdentified, "interview-worker", InterviewQuestionIdentifiedData{
		GenerationID: generationID,
		Question:     identified.Question,
		Provider:     identified.Provider,
		Model:        identified.Model,
	}))

	contextText := mem.interviewContextBlock()
	go w.streamAnswer(ctx, sessionID, generationID, identified.Question, contextText, "auto")
}

func (w *InterviewWorker) startHelp(parent context.Context, sessionID string, mem *sessionMemory, trigger, text string) {
	question := strings.TrimSpace(text)
	if question == "" {
		question = strings.TrimSpace(mem.InterviewQuestion)
	}
	if question == "" {
		question = mem.latestInterviewerText()
	}
	if trigger == "" {
		trigger = "button"
	}

	w.mu.Lock()
	if cancel := w.help[sessionID]; cancel != nil {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	generationID := NewID("interview-help")
	w.help[sessionID] = cancel
	w.helpGen[sessionID] = generationID
	w.mu.Unlock()

	_ = w.emit(ctx, NewEvent(sessionID, EventInterviewHelpStarted, "interview-worker", InterviewStartedData{
		GenerationID: generationID,
		Trigger:      trigger,
		Question:     question,
	}))
	if question == "" {
		message := "в транскрипте пока нет вопроса интервьюера"
		_ = w.emit(ctx, NewEvent(sessionID, EventError, "interview-worker", ErrorData{Where: "interview.help", Message: message}))
		_ = w.emit(ctx, NewEvent(sessionID, EventInterviewHelpCanceled, "interview-worker", InterviewCanceledData{GenerationID: generationID, Reason: "error"}))
		w.finishLane(sessionID, generationID, "help")
		return
	}
	go w.streamAnswer(ctx, sessionID, generationID, question, mem.interviewContextBlock(), "help")
}

func (w *InterviewWorker) streamAnswer(ctx context.Context, sessionID, generationID, question, contextText, lane string) {
	started := time.Now()
	deltaType := EventInterviewAutoDelta
	doneType := EventInterviewAutoDone
	canceledType := EventInterviewAutoCanceled
	if lane == "help" {
		deltaType = EventInterviewHelpDelta
		doneType = EventInterviewHelpDone
		canceledType = EventInterviewHelpCanceled
	}
	text, provider, model, err := w.llm.StreamInterviewAnswer(ctx, sessionID, contextText, question, lane, func(delta string) error {
		return w.emit(ctx, NewEvent(sessionID, deltaType, "interview-worker", InterviewDeltaData{GenerationID: generationID, Delta: delta}))
	})
	if ctx.Err() != nil {
		_ = w.emit(ctx, NewEvent(sessionID, canceledType, "interview-worker", InterviewCanceledData{GenerationID: generationID, Reason: "canceled"}))
		w.finishLane(sessionID, generationID, lane)
		return
	}
	if err != nil {
		w.logger.Warn("interview answer failed", "session_id", sessionID, "lane", lane, "generation_id", generationID, "error", err)
		_ = w.emit(ctx, NewEvent(sessionID, EventError, "interview-worker", ErrorData{Where: "interview." + lane, Message: err.Error()}))
		_ = w.emit(ctx, NewEvent(sessionID, canceledType, "interview-worker", InterviewCanceledData{GenerationID: generationID, Reason: "error"}))
		w.finishLane(sessionID, generationID, lane)
		return
	}
	w.logger.Info("interview answer done", "session_id", sessionID, "lane", lane, "generation_id", generationID, "elapsed_ms", time.Since(started).Milliseconds(), "provider", provider, "model", model)
	_ = w.emit(ctx, NewEvent(sessionID, doneType, "interview-worker", InterviewDoneData{
		GenerationID: generationID,
		Question:     question,
		Text:         text,
		Provider:     provider,
		Model:        model,
	}))
	w.finishLane(sessionID, generationID, lane)
}

func (w *InterviewWorker) finishLane(sessionID, generationID, lane string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if lane == "help" {
		if w.helpGen[sessionID] == generationID {
			delete(w.help, sessionID)
			delete(w.helpGen, sessionID)
		}
		return
	}
	if w.autoGen[sessionID] == generationID {
		delete(w.auto, sessionID)
		delete(w.autoGen, sessionID)
	}
}
