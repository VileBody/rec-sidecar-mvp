package clean

import (
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Role                      string
	HTTPAddr                  string
	NATSURL                   string
	SubjectPrefix             string
	CoachEnabled              bool
	LLMServiceURL             string
	LLMServiceToken           string
	LogLevel                  slog.Level
	MinSellerChars            int
	MinSellerGrowth           int
	MinStageChars             int
	MinStageGrowth            int
	StagePartialMinInterval   time.Duration
	LLMTimeout                time.Duration
	SellerTemperature         float64
	STTProvider               string
	InworldAPIKey             string
	InworldTTSBase            string
	InworldTTSModel           string
	InworldSTTWSURL           string
	InworldSTTModel           string
	InworldSTTLanguage        string
	InworldSTTDiarize         bool
	InworldSellerVoice        string
	InworldClientVoice        string
	InworldLanguage           string
	SonioxAPIKey              string
	SonioxSTTWSURL            string
	SonioxSTTModel            string
	SonioxAudioFormat         string
	SonioxLanguage            string
	SonioxLanguageStrict      bool
	SonioxDiarize             bool
	SonioxEndpointDetection   bool
	SonioxEndpointSensitivity float64
	SonioxMaxEndpointDelayMS  int
}

func ConfigFromEnv() Config {
	return Config{
		Role:                      env("CLEAN_START_ROLE", "gateway"),
		HTTPAddr:                  env("CLEAN_START_HTTP_ADDR", ":8110"),
		NATSURL:                   env("NATS_URL", "nats://127.0.0.1:4222"),
		SubjectPrefix:             strings.Trim(env("CLEAN_START_SUBJECT_PREFIX", "clean.session"), "."),
		CoachEnabled:              envBool("CLEAN_START_COACH_ENABLED", true),
		LLMServiceURL:             strings.TrimRight(env("COACH_LLM_SERVICE_URL", "http://127.0.0.1:8088"), "/"),
		LLMServiceToken:           env("COACH_LLM_SERVICE_TOKEN", ""),
		LogLevel:                  parseLogLevel(env("CLEAN_START_LOG_LEVEL", "info")),
		MinSellerChars:            envInt("CLEAN_START_MIN_SELLER_CHARS", 12),
		MinSellerGrowth:           envInt("CLEAN_START_MIN_SELLER_GROWTH", 12),
		MinStageChars:             envInt("CLEAN_START_MIN_STAGE_CHARS", 18),
		MinStageGrowth:            envInt("CLEAN_START_MIN_STAGE_GROWTH", 24),
		StagePartialMinInterval:   time.Duration(envInt("CLEAN_START_STAGE_PARTIAL_INTERVAL_MS", 2200)) * time.Millisecond,
		LLMTimeout:                time.Duration(envInt("CLEAN_START_LLM_TIMEOUT_SECS", 30)) * time.Second,
		SellerTemperature:         float64(envInt("CLEAN_START_SELLER_TEMPERATURE_X100", 35)) / 100,
		STTProvider:               strings.ToLower(env("CLEAN_START_STT_PROVIDER", "auto")),
		InworldAPIKey:             env("INWORLD_API_KEY", ""),
		InworldTTSBase:            strings.TrimRight(env("INWORLD_TTS_API_BASE", "https://api.inworld.ai"), "/"),
		InworldTTSModel:           env("INWORLD_TTS_MODEL", "inworld-tts-1"),
		InworldSTTWSURL:           env("INWORLD_STT_WS_URL", "wss://api.inworld.ai/stt/v1/transcribe:streamBidirectional"),
		InworldSTTModel:           env("INWORLD_STT_MODEL", "soniox/stt-rt-v4"),
		InworldSTTLanguage:        env("INWORLD_STT_LANGUAGE", "ru"),
		InworldSTTDiarize:         envBool("INWORLD_STT_SPEAKER_DIARIZATION", true),
		InworldSellerVoice:        env("INWORLD_TTS_SELLER_VOICE", "Elena"),
		InworldClientVoice:        env("INWORLD_TTS_CLIENT_VOICE", "Arkady"),
		InworldLanguage:           env("INWORLD_TTS_LANGUAGE", "ru-RU"),
		SonioxAPIKey:              env("SONIOX_API_KEY", ""),
		SonioxSTTWSURL:            env("SONIOX_STT_WS_URL", "wss://stt-rt.soniox.com/transcribe-websocket"),
		SonioxSTTModel:            env("SONIOX_STT_MODEL", "stt-rt-v5"),
		SonioxAudioFormat:         env("SONIOX_AUDIO_FORMAT", "s16le"),
		SonioxLanguage:            env("SONIOX_LANGUAGE", "ru"),
		SonioxLanguageStrict:      envBool("SONIOX_LANGUAGE_STRICT", false),
		SonioxDiarize:             envBool("SONIOX_SPEAKER_DIARIZATION", true),
		SonioxEndpointDetection:   envBool("SONIOX_ENDPOINT_DETECTION", false),
		SonioxEndpointSensitivity: envFloat("SONIOX_ENDPOINT_SENSITIVITY", 0),
		SonioxMaxEndpointDelayMS:  envInt("SONIOX_MAX_ENDPOINT_DELAY_MS", 2000),
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

func envBool(name string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if value == "" {
		return fallback
	}
	switch value {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return fallback
	}
}

func envFloat(name string, fallback float64) float64 {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
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
