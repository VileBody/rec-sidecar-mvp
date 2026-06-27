package clean

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/nats-io/nats.go"
)

type ScorecardWorker struct {
	cfg    Config
	nc     *nats.Conn
	logger *slog.Logger
	sub    *nats.Subscription
}

type stageScorecardPayload struct {
	Readiness      string `json:"readiness"`
	ReadinessLabel string `json:"readiness_label"`
	ReadyToAdvance bool   `json:"ready_to_advance"`
	NextAction     string `json:"next_action"`
	Summary        string `json:"summary"`
}

func NewScorecardWorker(cfg Config, nc *nats.Conn, logger *slog.Logger) *ScorecardWorker {
	return &ScorecardWorker{cfg: cfg, nc: nc, logger: logger.With("component", "scorecard-worker")}
}

func (w *ScorecardWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectTypeWildcard(w.cfg.SubjectPrefix, EventStageCommitted), func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		event = EventWithNATSHeaders(event, msg.Header)
		handleCtx, span := StartEventSpan(ctx, event, "scorecard_worker.handle_event")
		defer EndSpan(span, nil)
		stage, err := DecodeData[StageData](event)
		if err != nil {
			return
		}
		scorecard := scorecardFromStage(stage)
		_ = PublishEventWithContext(handleCtx, w.nc, w.cfg, NewEvent(event.SessionID, EventScorecardUpdate, "scorecard-worker", scorecard))
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
		var payload stageScorecardPayload
		_ = json.Unmarshal(stage.Scorecard, &payload)
		if payload.Readiness == "" {
			payload.Readiness = "pending"
		}
		if payload.ReadinessLabel == "" {
			payload.ReadinessLabel = "Из LLM scorecard"
		}
		if payload.NextAction == "" {
			payload.NextAction = stage.Step
		}
		if payload.Summary == "" {
			payload.Summary = "Scorecard пришел вместе со stage response."
		}
		return ScorecardData{
			Readiness:      payload.Readiness,
			ReadinessLabel: payload.ReadinessLabel,
			ReadyToAdvance: payload.ReadyToAdvance,
			NextAction:     payload.NextAction,
			Summary:        payload.Summary,
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
