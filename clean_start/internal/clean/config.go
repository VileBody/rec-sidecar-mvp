package clean

import (
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Role              string
	HTTPAddr          string
	NATSURL           string
	SubjectPrefix     string
	LLMServiceURL     string
	LLMServiceToken   string
	LogLevel          slog.Level
	MinSellerChars    int
	MinSellerGrowth   int
	MinStageChars     int
	LLMTimeout        time.Duration
	SellerTemperature float64
}

func ConfigFromEnv() Config {
	return Config{
		Role:              env("CLEAN_START_ROLE", "gateway"),
		HTTPAddr:          env("CLEAN_START_HTTP_ADDR", ":8110"),
		NATSURL:           env("NATS_URL", "nats://127.0.0.1:4222"),
		SubjectPrefix:     strings.Trim(env("CLEAN_START_SUBJECT_PREFIX", "clean.session"), "."),
		LLMServiceURL:     strings.TrimRight(env("COACH_LLM_SERVICE_URL", "http://127.0.0.1:8088"), "/"),
		LLMServiceToken:   env("COACH_LLM_SERVICE_TOKEN", ""),
		LogLevel:          parseLogLevel(env("CLEAN_START_LOG_LEVEL", "info")),
		MinSellerChars:    envInt("CLEAN_START_MIN_SELLER_CHARS", 12),
		MinSellerGrowth:   envInt("CLEAN_START_MIN_SELLER_GROWTH", 12),
		MinStageChars:     envInt("CLEAN_START_MIN_STAGE_CHARS", 18),
		LLMTimeout:        time.Duration(envInt("CLEAN_START_LLM_TIMEOUT_SECS", 30)) * time.Second,
		SellerTemperature: float64(envInt("CLEAN_START_SELLER_TEMPERATURE_X100", 35)) / 100,
	}
}

func env(name, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func parseLogLevel(value string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
