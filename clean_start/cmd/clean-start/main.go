package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/VileBody/rec-sidecar-mvp/clean_start/internal/clean"
	"github.com/nats-io/nats.go"
)

func main() {
	roleFlag := flag.String("role", "", "gateway, seller-worker, assist-worker, stage-worker, scorecard-worker, student-worker, test-agent, or all")
	flag.Parse()

	cfg := clean.ConfigFromEnv()
	if *roleFlag != "" {
		cfg.Role = *roleFlag
	}
	if cfg.OTelServiceName == "" || cfg.OTelServiceName == "clean-start-gateway" {
		cfg.OTelServiceName = "clean-start-" + cfg.Role
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel}))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdownTelemetry, err := clean.InitTelemetry(ctx, cfg, logger)
	if err != nil {
		logger.Error("otel init failed", "error", err)
		os.Exit(1)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdownTelemetry(shutdownCtx); err != nil {
			logger.Warn("otel shutdown failed", "error", err)
		}
	}()
	shutdownMetrics := clean.StartMetricsServer(ctx, cfg, logger)
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdownMetrics(shutdownCtx); err != nil {
			logger.Warn("metrics shutdown failed", "error", err)
		}
	}()

	llm := clean.NewLLMClient(cfg, logger)
	inworld := clean.NewInworldClient(cfg)
	var nc *nats.Conn
	if cfg.Role != "test-agent" {
		var err error
		nc, err = clean.ConnectNATS(cfg, logger)
		if err != nil {
			logger.Error("nats connect failed", "error", err)
			os.Exit(1)
		}
		defer nc.Drain()
	}

	var runners []clean.Runner
	switch cfg.Role {
	case "gateway":
		runners = append(runners, clean.NewGateway(cfg, nc, inworld, logger))
	case "seller-worker":
		if cfg.CoachEnabled {
			runners = append(runners, clean.NewSellerWorker(cfg, nc, llm, logger))
		} else {
			runners = append(runners, clean.NewIdleRunner(cfg.Role, logger))
		}
	case "assist-worker":
		if cfg.CoachEnabled {
			runners = append(runners, clean.NewAssistWorker(cfg, nc, llm, logger))
		} else {
			runners = append(runners, clean.NewIdleRunner(cfg.Role, logger))
		}
	case "stage-worker":
		if cfg.CoachEnabled {
			runners = append(runners, clean.NewStageWorker(cfg, nc, llm, logger))
		} else {
			runners = append(runners, clean.NewIdleRunner(cfg.Role, logger))
		}
	case "scorecard-worker":
		if cfg.CoachEnabled {
			runners = append(runners, clean.NewScorecardWorker(cfg, nc, logger))
		} else {
			runners = append(runners, clean.NewIdleRunner(cfg.Role, logger))
		}
	case "student-worker":
		runners = append(runners, clean.NewStudentWorker(cfg, nc, llm, logger))
	case "test-agent":
		runners = append(runners, clean.NewTestAgentGateway(cfg, llm, inworld, logger))
	case "all":
		runners = append(runners, clean.NewGateway(cfg, nc, inworld, logger))
		if cfg.CoachEnabled {
			runners = append(
				runners,
				clean.NewSellerWorker(cfg, nc, llm, logger),
				clean.NewAssistWorker(cfg, nc, llm, logger),
				clean.NewStageWorker(cfg, nc, llm, logger),
				clean.NewScorecardWorker(cfg, nc, logger),
			)
		}
		runners = append(runners, clean.NewStudentWorker(cfg, nc, llm, logger))
	default:
		logger.Error("unknown role", "role", cfg.Role)
		os.Exit(2)
	}

	errCh := make(chan error, len(runners))
	for _, runner := range runners {
		go func(r clean.Runner) {
			errCh <- r.Run(ctx)
		}(runner)
	}

	select {
	case <-ctx.Done():
		logger.Info("shutdown requested")
	case err := <-errCh:
		if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("runner stopped", "error", err)
			os.Exit(1)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	for _, runner := range runners {
		if err := runner.Shutdown(shutdownCtx); err != nil {
			logger.Warn("runner shutdown failed", "error", err)
		}
	}
}
