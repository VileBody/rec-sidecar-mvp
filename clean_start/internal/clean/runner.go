package clean

import (
	"context"
	"log/slog"
)

type Runner interface {
	Run(context.Context) error
	Shutdown(context.Context) error
}

type IdleRunner struct {
	role   string
	logger *slog.Logger
}

func NewIdleRunner(role string, logger *slog.Logger) *IdleRunner {
	return &IdleRunner{role: role, logger: logger.With("component", role)}
}

func (r *IdleRunner) Run(ctx context.Context) error {
	r.logger.Info("coach disabled; runner idle", "role", r.role)
	<-ctx.Done()
	return ctx.Err()
}

func (r *IdleRunner) Shutdown(context.Context) error {
	return nil
}
