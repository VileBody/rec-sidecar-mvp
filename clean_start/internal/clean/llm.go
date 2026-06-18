package clean

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

type LLMClient struct {
	cfg    Config
	client *http.Client
	logger *slog.Logger
}

type streamEvent struct {
	Event   string `json:"event"`
	Text    string `json:"text,omitempty"`
	Model   string `json:"model,omitempty"`
	Message string `json:"message,omitempty"`
}

func NewLLMClient(cfg Config, logger *slog.Logger) *LLMClient {
	return &LLMClient{
		cfg: cfg,
		client: &http.Client{
			Timeout: cfg.LLMTimeout,
		},
		logger: logger.With("component", "llm-client"),
	}
}

func (c *LLMClient) StreamSeller(ctx context.Context, sessionID, contextText, question string, onDelta func(string) error) (string, string, string, error) {
	if c.cfg.LLMServiceURL == "" {
		text := fallbackSeller(contextText)
		return text, "fallback", "local", onDelta(text)
	}
	body := map[string]any{
		"id":       time.Now().UnixNano(),
		"run_id":   sessionID,
		"context":  contextText,
		"question": question,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/chat/stream", bytes.NewReader(raw))
	if err != nil {
		return "", "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		text := fallbackSeller(contextText)
		_ = onDelta(text)
		return text, "fallback", "local-after-error", nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		text := fallbackSeller(contextText)
		_ = onDelta(text)
		return text, "fallback", fmt.Sprintf("http-%d", resp.StatusCode), nil
	}

	var parts []string
	model := ""
	if err := scanSSE(resp.Body, func(event streamEvent) error {
		switch event.Event {
		case "model":
			model = event.Model
		case "delta":
			if event.Text != "" {
				parts = append(parts, event.Text)
				return onDelta(event.Text)
			}
		case "error":
			return errors.New(event.Message)
		}
		return nil
	}); err != nil {
		if len(parts) == 0 {
			text := fallbackSeller(contextText)
			_ = onDelta(text)
			return text, "fallback", "local-after-stream-error", nil
		}
		return strings.Join(parts, ""), "llm-helper", model, nil
	}
	return strings.Join(parts, ""), "llm-helper", model, nil
}

func (c *LLMClient) DetectStage(ctx context.Context, sessionID, contextText, currentStage string) (*StageData, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackStage(currentStage), nil
	}
	body := map[string]any{
		"run_id":        sessionID,
		"context":       contextText,
		"current_stage": currentStage,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/stage", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fallbackStage(currentStage), nil
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallbackStage(currentStage), nil
	}
	var stage StageData
	if err := json.NewDecoder(resp.Body).Decode(&stage); err != nil {
		return fallbackStage(currentStage), nil
	}
	return &stage, nil
}

func scanSSE(reader io.Reader, handle func(streamEvent) error) error {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var dataLines []string
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if len(dataLines) > 0 {
				var event streamEvent
				if err := json.Unmarshal([]byte(strings.Join(dataLines, "\n")), &event); err != nil {
					return err
				}
				if err := handle(event); err != nil {
					return err
				}
				dataLines = dataLines[:0]
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	return scanner.Err()
}

func fallbackSeller(contextText string) string {
	lower := strings.ToLower(contextText)
	if strings.Contains(lower, "сомнева") || strings.Contains(lower, "проходил") {
		return "Понял ваш скепсис, тогда давайте без общих обещаний: какой конкретный результат вы не получили в прошлых форматах и что для вас было бы доказательством, что этот подход не повторит тот же сценарий?"
	}
	return "Давайте зафиксируем текущую ситуацию: какая у вас сейчас основная задача в бизнесе и какой результат вы хотите получить в ближайшие 90 дней?"
}

func fallbackStage(current string) *StageData {
	if current == "" || current == "S2.1" {
		return &StageData{
			Stage:    "S2.2",
			Title:    "Квалификация: текущая ситуация",
			Agenda:   "узнать текущую ситуацию, боли и ограничения",
			Step:     "добрать факты текущей ситуации и конкретные ограничения",
			Provider: "fallback",
			Model:    "local-heuristic",
		}
	}
	return &StageData{Stage: current, Provider: "fallback", Model: "last-known-stage"}
}
