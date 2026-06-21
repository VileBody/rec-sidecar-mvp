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

func NewScorecardWorker(cfg Config, nc *nats.Conn, logger *slog.Logger) *ScorecardWorker {
	return &ScorecardWorker{cfg: cfg, nc: nc, logger: logger.With("component", "scorecard-worker")}
}

func (w *ScorecardWorker) Run(ctx context.Context) error {
	sub, err := w.nc.Subscribe(SubjectTypeWildcard(w.cfg.SubjectPrefix, EventStageCommitted), func(msg *nats.Msg) {
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
