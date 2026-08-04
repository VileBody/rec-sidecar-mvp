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

type StageWorker struct {
	cfg    Config
	nc     *nats.Conn
	llm    *LLMClient
	logger *slog.Logger
	memory *memoryBook
	sub    *nats.Subscription
	mu     sync.Mutex
	states map[string]*stageSessionState
}

type stageSessionState struct {
	inFlight        bool
	pending         *stageDetectionRequest
	lastStarted     time.Time
	lastPartialText string
}

type stageDetectionRequest struct {
	sessionID string
	mem       *sessionMemory
	eventType string
	text      string
	force     bool
}

func NewStageWorker(cfg Config, nc *nats.Conn, llm *LLMClient, logger *slog.Logger) *StageWorker {
	return &StageWorker{
		cfg:    cfg,
		nc:     nc,
		llm:    llm,
		logger: logger.With("component", "stage-worker"),
		memory: newMemoryBook(),
		states: make(map[string]*stageSessionState),
	}
}

func (w *StageWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectWildcard(w.cfg.SubjectPrefix), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		event = EventWithNATSHeaders(event, msg.Header)
		handleCtx, span := StartEventSpan(ctx, event, "stage_worker.handle_event")
		defer EndSpan(span, nil)
		mem := w.memory.apply(event)
		switch event.Type {
		case EventClientPartial:
			data, _ := DecodeData[TextData](event)
			w.scheduleDetect(handleCtx, event.SessionID, mem, EventStageCandidate, data.Text, false)
		case EventClientFinal:
			data, _ := DecodeData[TextData](event)
			w.scheduleDetect(handleCtx, event.SessionID, mem, EventStageCommitted, data.Text, true)
		case EventSTTPartial:
			data, _ := DecodeData[SpeechData](event)
			if shouldRunSalesCoachForSpeech(data) {
				w.scheduleDetect(handleCtx, event.SessionID, mem, EventStageCandidate, data.Text, false)
			}
		case EventSTTFinal:
			data, _ := DecodeData[SpeechData](event)
			if shouldRunSalesCoachForSpeech(data) {
				w.scheduleDetect(handleCtx, event.SessionID, mem, EventStageCommitted, data.Text, true)
			}
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

func (w *StageWorker) publishPipelineStatusContext(ctx context.Context, sessionID string, data PipelineStatusData) {
	if data.Component == "" || data.Status == "" {
		return
	}
	data.TraceID = traceIDFromContext(ctx)
	_ = PublishEventWithContext(ctx, w.nc, w.cfg, NewEvent(sessionID, EventPipelineStatus, "stage-worker", data))
}

func (w *StageWorker) scheduleDetect(ctx context.Context, sessionID string, mem *sessionMemory, eventType string, text string, force bool) {
	cleanText := strings.TrimSpace(text)
	w.mu.Lock()
	state := w.stageStateLocked(sessionID)
	if !force {
		if !w.shouldUsePartialLocked(state, cleanText) {
			w.mu.Unlock()
			return
		}
		if w.cfg.StagePartialMinInterval > 0 && !state.lastStarted.IsZero() && time.Since(state.lastStarted) < w.cfg.StagePartialMinInterval {
			w.mu.Unlock()
			return
		}
	}

	req := stageDetectionRequest{
		sessionID: sessionID,
		mem:       mem,
		eventType: eventType,
		text:      cleanText,
		force:     force,
	}
	if state.inFlight {
		state.pending = &req
		w.mu.Unlock()
		w.publishPipelineStatusContext(ctx, sessionID, PipelineStatusData{Component: "stage", Status: "queued", Trigger: eventType, Detail: "предыдущий stage detect еще выполняется"})
		return
	}
	w.startStageLocked(ctx, state, req)
	w.mu.Unlock()
}

func (w *StageWorker) stageStateLocked(sessionID string) *stageSessionState {
	state := w.states[sessionID]
	if state == nil {
		state = &stageSessionState{}
		w.states[sessionID] = state
	}
	return state
}

func (w *StageWorker) shouldUsePartialLocked(state *stageSessionState, cleanText string) bool {
	if len([]rune(cleanText)) < w.cfg.MinStageChars {
		return false
	}
	prev := state.lastPartialText
	if prev == cleanText {
		return false
	}
	growth := len([]rune(cleanText)) - len([]rune(prev))
	if prev != "" && strings.HasPrefix(cleanText, prev) && growth < w.cfg.MinStageGrowth && !endsStageSentence(cleanText) {
		return false
	}
	state.lastPartialText = cleanText
	return true
}

func endsStageSentence(text string) bool {
	return strings.HasSuffix(text, ".") || strings.HasSuffix(text, "?") || strings.HasSuffix(text, "!") || strings.HasSuffix(text, "…")
}

func (w *StageWorker) startStageLocked(ctx context.Context, state *stageSessionState, req stageDetectionRequest) {
	state.inFlight = true
	state.lastStarted = time.Now()
	go w.detectThenContinue(ctx, req)
}

func (w *StageWorker) detectThenContinue(ctx context.Context, req stageDetectionRequest) {
	w.detect(ctx, req.sessionID, req.mem, req.eventType)

	w.mu.Lock()
	defer w.mu.Unlock()
	state := w.states[req.sessionID]
	if state == nil {
		return
	}
	state.inFlight = false
	if ctx.Err() != nil || state.pending == nil {
		return
	}
	next := *state.pending
	state.pending = nil
	if !next.force && w.cfg.StagePartialMinInterval > 0 && time.Since(state.lastStarted) < w.cfg.StagePartialMinInterval {
		return
	}
	w.startStageLocked(ctx, state, next)
}

func (w *StageWorker) detect(ctx context.Context, sessionID string, mem *sessionMemory, eventType string) {
	started := time.Now()
	includeScorecard := eventType == EventStageCommitted
	w.publishPipelineStatusContext(ctx, sessionID, PipelineStatusData{Component: "stage", Status: "sent", Trigger: eventType, Detail: "отправили stage detect"})
	stage, err := w.llm.DetectStage(ctx, sessionID, mem.contextBlock(), mem.CurrentStage, includeScorecard)
	if err != nil {
		w.publishPipelineStatusContext(ctx, sessionID, PipelineStatusData{Component: "stage", Status: "error", Trigger: eventType, Detail: err.Error(), ElapsedMS: time.Since(started).Milliseconds()})
		_ = PublishEventWithContext(ctx, w.nc, w.cfg, NewEvent(sessionID, EventError, "stage-worker", ErrorData{Where: "stage", Message: err.Error()}))
		return
	}
	if stage == nil {
		w.publishPipelineStatusContext(ctx, sessionID, PipelineStatusData{Component: "stage", Status: "skipped", Trigger: eventType, Detail: "stage не изменился", ElapsedMS: time.Since(started).Milliseconds()})
		return
	}
	w.logger.Info("stage detected", "session_id", sessionID, "stage", stage.Stage, "event", eventType, "include_scorecard", includeScorecard, "elapsed_ms", time.Since(started).Milliseconds())
	w.publishPipelineStatusContext(ctx, sessionID, PipelineStatusData{Component: "stage", Status: "received", Trigger: eventType, Detail: stage.Stage, Provider: stage.Provider, Model: stage.Model, ElapsedMS: time.Since(started).Milliseconds()})
	_ = PublishEventWithContext(ctx, w.nc, w.cfg, NewEvent(sessionID, eventType, "stage-worker", stage))
}
