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

type helpOpenerResponse struct {
	Text     string `json:"text"`
	Model    string `json:"model"`
	Fallback bool   `json:"fallback"`
}

type liveSellerResponse struct {
	Action   string `json:"action"`
	Text     string `json:"text"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
}

type readyGateResponse struct {
	ClientRevision     int64   `json:"client_revision"`
	Action             string  `json:"action"`
	Confidence         float64 `json:"confidence"`
	Reason             string  `json:"reason"`
	Readiness          string  `json:"readiness"`
	SemanticType       string  `json:"semantic_type"`
	MutexDecision      string  `json:"mutex_decision"`
	GenerationBrief    string  `json:"generation_brief"`
	LatestClientIntent string  `json:"latest_client_intent"`
	Provider           string  `json:"provider"`
	Model              string  `json:"model"`
}

type pivotGateResponse struct {
	ClientRevision      int64   `json:"client_revision"`
	Status              string  `json:"status"`
	Confidence          float64 `json:"confidence"`
	Reason              string  `json:"reason"`
	PivotType           string  `json:"pivot_type"`
	SetsPendingReplan   bool    `json:"sets_pending_replan"`
	ClearsPendingReplan bool    `json:"clears_pending_replan"`
	ReplanLevel         string  `json:"replan_level"`
	LatestClientIntent  string  `json:"latest_client_intent"`
	BaseClientIntent    string  `json:"base_client_intent"`
	Provider            string  `json:"provider"`
	Model               string  `json:"model"`
}

type studentTranslateResponse struct {
	Text     string `json:"text"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
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
	InjectTraceHeaders(ctx, req)
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

func (c *LLMClient) LiveSellerSuggestion(ctx context.Context, sessionID, contextText, currentText string, force bool) (liveSellerResponse, error) {
	if c.cfg.LLMServiceURL == "" {
		return liveSellerResponse{
			Action:   "suggest",
			Text:     fallbackSeller(contextText),
			Provider: "fallback",
			Model:    "local",
		}, nil
	}
	body := map[string]any{
		"run_id":       sessionID,
		"content":      contextText,
		"current_text": currentText,
		"force":        force,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/live", bytes.NewReader(raw))
	if err != nil {
		return liveSellerResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return liveSellerResponse{
			Action:   "suggest",
			Text:     fallbackSeller(contextText),
			Provider: "fallback",
			Model:    "local-after-error",
		}, nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return liveSellerResponse{
			Action:   "suggest",
			Text:     fallbackSeller(contextText),
			Provider: "fallback",
			Model:    fmt.Sprintf("http-%d", resp.StatusCode),
		}, nil
	}

	var out liveSellerResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return liveSellerResponse{}, err
	}
	out.Action = strings.TrimSpace(out.Action)
	out.Text = strings.TrimSpace(out.Text)
	out.Provider = strings.TrimSpace(out.Provider)
	out.Model = strings.TrimSpace(out.Model)
	if out.Action == "" {
		out.Action = "suggest"
	}
	if out.Provider == "" {
		out.Provider = "llm-helper"
	}
	return out, nil
}

func (c *LLMClient) ReadySellerGate(ctx context.Context, sessionID, contextText, currentText string, clientRevision int64) (readyGateResponse, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackReadyGate(clientRevision, contextText, currentText), nil
	}
	body := map[string]any{
		"run_id":          sessionID,
		"content":         contextText,
		"current_text":    currentText,
		"client_revision": clientRevision,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/live/ready-gate", bytes.NewReader(raw))
	if err != nil {
		return readyGateResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fallbackReadyGate(clientRevision, contextText, currentText), nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallbackReadyGate(clientRevision, contextText, currentText), nil
	}
	var out readyGateResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return readyGateResponse{}, err
	}
	return normalizeReadyGate(out, clientRevision), nil
}

func (c *LLMClient) PivotSellerGate(ctx context.Context, sessionID, contextText, currentText, activeGenerationID, baseClientText, pendingReplanState string, clientRevision int64) (pivotGateResponse, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackPivotGate(clientRevision), nil
	}
	body := map[string]any{
		"run_id":               sessionID,
		"content":              contextText,
		"current_text":         currentText,
		"client_revision":      clientRevision,
		"active_generation_id": activeGenerationID,
		"base_client_text":     baseClientText,
		"pending_replan_state": pendingReplanState,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/live/pivot-gate", bytes.NewReader(raw))
	if err != nil {
		return pivotGateResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fallbackPivotGate(clientRevision), nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallbackPivotGate(clientRevision), nil
	}
	var out pivotGateResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return pivotGateResponse{}, err
	}
	return normalizePivotGate(out, clientRevision), nil
}

func (c *LLMClient) DetectStage(ctx context.Context, sessionID, contextText, currentStage string, includeScorecard bool) (*StageData, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackStage(currentStage), nil
	}
	body := map[string]any{
		"run_id":            sessionID,
		"context":           contextText,
		"current_stage":     currentStage,
		"include_scorecard": includeScorecard,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/stage", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
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

func (c *LLMClient) HelpOpener(ctx context.Context, sessionID, contextText string) (string, string, bool, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackAssistFast(contextText), "local", true, nil
	}
	body := map[string]any{
		"id":      time.Now().UnixNano(),
		"run_id":  sessionID,
		"context": contextText,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/help/opener", bytes.NewReader(raw))
	if err != nil {
		return "", "", false, err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fallbackAssistFast(contextText), "local-after-error", true, nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallbackAssistFast(contextText), fmt.Sprintf("http-%d", resp.StatusCode), true, nil
	}
	var out helpOpenerResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return fallbackAssistFast(contextText), "local-after-decode-error", true, nil
	}
	return strings.TrimSpace(out.Text), out.Model, out.Fallback, nil
}

func (c *LLMClient) StreamHelpConstructive(ctx context.Context, sessionID, contextText string, onDelta func(string) error) (string, string, error) {
	if c.cfg.LLMServiceURL == "" {
		text := fallbackAssistSlow(contextText)
		return text, "local", onDelta(text)
	}
	body := map[string]any{
		"id":      time.Now().UnixNano(),
		"run_id":  sessionID,
		"context": contextText,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/coach/help/constructive/stream", bytes.NewReader(raw))
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		text := fallbackAssistSlow(contextText)
		_ = onDelta(text)
		return text, "local-after-error", nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		text := fallbackAssistSlow(contextText)
		_ = onDelta(text)
		return text, fmt.Sprintf("http-%d", resp.StatusCode), nil
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
			text := fallbackAssistSlow(contextText)
			_ = onDelta(text)
			return text, "local-after-stream-error", nil
		}
		return strings.Join(parts, ""), model, nil
	}
	return strings.Join(parts, ""), model, nil
}

func (c *LLMClient) GenerateClientReply(ctx context.Context, sessionID, contextText, sellerTranscript string) (string, string, string, error) {
	if c.cfg.LLMServiceURL == "" {
		return fallbackClientReply(contextText + "\n" + sellerTranscript), "fallback", "local", nil
	}
	question := "Ты вредный, скептичный, но реалистичный клиент на продаже high-check B2C ивента. Ответь на последнюю реплику продавца одной живой русской репликой, без markdown, без роли, 1-2 предложения."
	if strings.TrimSpace(sellerTranscript) != "" {
		contextText += "\n--- Последняя реплика продавца, которую услышал клиент ---\n" + sellerTranscript + "\n"
	}
	return c.StreamSeller(ctx, sessionID, contextText, question, func(string) error { return nil })
}

func (c *LLMClient) StudentTranslate(ctx context.Context, sessionID, text, direction string) (string, string, string, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		return "", "fallback", "empty", nil
	}
	if c.cfg.LLMServiceURL == "" {
		return fallbackStudentTranslation(text, direction), "fallback", "local", nil
	}
	body := map[string]any{
		"run_id":    sessionID,
		"text":      text,
		"direction": direction,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/student/translate", bytes.NewReader(raw))
	if err != nil {
		return "", "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fallbackStudentTranslation(text, direction), "fallback", "local-after-error", nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallbackStudentTranslation(text, direction), "fallback", fmt.Sprintf("http-%d", resp.StatusCode), nil
	}
	var out studentTranslateResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", "", "", err
	}
	return strings.TrimSpace(out.Text), strings.TrimSpace(out.Provider), strings.TrimSpace(out.Model), nil
}

func (c *LLMClient) StreamStudentAnswer(ctx context.Context, sessionID, contextText, question string, onDelta func(string) error) (string, string, error) {
	if c.cfg.LLMServiceURL == "" {
		text := fallbackStudentAnswer(contextText, question)
		return text, "local", onDelta(text)
	}
	body := map[string]any{
		"id":       time.Now().UnixNano(),
		"run_id":   sessionID,
		"context":  contextText,
		"question": question,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.LLMServiceURL+"/v1/student/answer/stream", bytes.NewReader(raw))
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	InjectTraceHeaders(ctx, req)
	if c.cfg.LLMServiceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.LLMServiceToken)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		text := fallbackStudentAnswer(contextText, question)
		_ = onDelta(text)
		return text, "local-after-error", nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		text := fallbackStudentAnswer(contextText, question)
		_ = onDelta(text)
		return text, fmt.Sprintf("http-%d", resp.StatusCode), nil
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
			text := fallbackStudentAnswer(contextText, question)
			_ = onDelta(text)
			return text, "local-after-stream-error", nil
		}
		return strings.Join(parts, ""), model, nil
	}
	return strings.Join(parts, ""), model, nil
}

func scanSSE(reader io.Reader, handle func(streamEvent) error) error {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var dataLines []string
	flush := func() error {
		if len(dataLines) == 0 {
			return nil
		}
		var event streamEvent
		if err := json.Unmarshal([]byte(strings.Join(dataLines, "\n")), &event); err != nil {
			return err
		}
		if err := handle(event); err != nil {
			return err
		}
		dataLines = dataLines[:0]
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := flush(); err != nil {
				return err
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return flush()
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

func fallbackAssistFast(contextText string) string {
	lower := strings.ToLower(contextText)
	if strings.Contains(lower, "сомнева") || strings.Contains(lower, "страш") {
		return "Понимаю, сомнения здесь нормальны, особенно если прошлый опыт не дал результата."
	}
	return "Да, понял вас, давайте спокойно разберем ситуацию по фактам."
}

func fallbackAssistSlow(contextText string) string {
	lower := strings.ToLower(contextText)
	if strings.Contains(lower, "сомнева") || strings.Contains(lower, "не дохожу") {
		return "Уточните, что именно обычно ломает внедрение: время, поддержка, дисциплина или непонятный план действий?"
	}
	return "Задайте один уточняющий вопрос про текущую цель клиента и критерий, по которому он поймет, что участие окупилось."
}

func fallbackClientReply(contextText string) string {
	lower := strings.ToLower(contextText)
	if strings.Contains(lower, "договор") || strings.Contains(lower, "куп") || strings.Contains(lower, "билет") {
		return "Я не против посмотреть, но пока не понимаю, почему это не очередная встреча с красивыми обещаниями."
	}
	if strings.Contains(lower, "план") || strings.Contains(lower, "внедр") {
		return "Сомневаюсь, что я реально дойду до внедрения, у меня уже было несколько форматов, где все заканчивалось конспектами."
	}
	return "Честно, пока звучит интересно, но я не понимаю, зачем мне тратить на это время именно сейчас."
}

func fallbackReadyGate(clientRevision int64, contextText, currentText string) readyGateResponse {
	action := "WAIT"
	readiness := "incomplete"
	mutexDecision := "DO_NOT_LOCK"
	reason := "fallback ready gate waits for clearer client intent"
	if strings.TrimSpace(contextText) != "" {
		if strings.TrimSpace(currentText) == "" {
			action = "GENERATE"
			readiness = "actionable"
			mutexDecision = "LOCK_AND_GENERATE"
			reason = "fallback ready gate has actionable context and no visible reply"
		} else {
			action = "KEEP"
			readiness = "meaningful_but_covered"
			reason = "fallback ready gate keeps current visible reply"
		}
	}
	return readyGateResponse{
		ClientRevision: clientRevision,
		Action:         action,
		Confidence:     1,
		Reason:         reason,
		Readiness:      readiness,
		SemanticType:   "other",
		MutexDecision:  mutexDecision,
		Provider:       "fallback",
		Model:          "local",
	}
}

func fallbackPivotGate(clientRevision int64) pivotGateResponse {
	return pivotGateResponse{
		ClientRevision:      clientRevision,
		Status:              "WAIT_NOISE",
		Confidence:          1,
		Reason:              "fallback pivot gate does not change pending replan",
		PivotType:           "none",
		SetsPendingReplan:   false,
		ClearsPendingReplan: false,
		ReplanLevel:         "none",
		Provider:            "fallback",
		Model:               "local",
	}
}

func normalizeReadyGate(out readyGateResponse, fallbackRevision int64) readyGateResponse {
	if out.ClientRevision == 0 {
		out.ClientRevision = fallbackRevision
	}
	out.Action = strings.ToUpper(strings.TrimSpace(out.Action))
	switch out.Action {
	case "SUGGEST", "GENERATE", "ON":
		out.Action = "GENERATE"
	case "SKIP", "KEEP":
		out.Action = "KEEP"
	case "WAIT":
	default:
		out.Action = "WAIT"
	}
	out.Readiness = strings.ToLower(strings.TrimSpace(out.Readiness))
	out.SemanticType = strings.ToLower(strings.TrimSpace(out.SemanticType))
	out.MutexDecision = strings.ToUpper(strings.TrimSpace(out.MutexDecision))
	if out.Action == "GENERATE" {
		out.MutexDecision = "LOCK_AND_GENERATE"
		if out.Readiness == "" {
			out.Readiness = "actionable"
		}
	} else {
		out.MutexDecision = "DO_NOT_LOCK"
	}
	if out.Confidence == 0 && out.Action == "GENERATE" {
		out.Confidence = 1
	}
	if out.Provider == "" {
		out.Provider = "llm-helper"
	}
	return out
}

func normalizePivotGate(out pivotGateResponse, fallbackRevision int64) pivotGateResponse {
	if out.ClientRevision == 0 {
		out.ClientRevision = fallbackRevision
	}
	out.Status = strings.ToUpper(strings.TrimSpace(out.Status))
	switch out.Status {
	case "SUGGEST", "GENERATE", "INVALIDATED", "CHANGE_HARD":
		out.Status = "CHANGE_HARD"
	case "SKIP", "VALID", "NO_CHANGE":
		out.Status = "NO_CHANGE"
	case "ADAPT_SOFT", "SOFT":
		out.Status = "ADAPT_SOFT"
	case "WAIT", "WAIT_NOISE":
		out.Status = "WAIT_NOISE"
	default:
		out.Status = "WAIT_NOISE"
	}
	out.PivotType = strings.ToLower(strings.TrimSpace(out.PivotType))
	out.ReplanLevel = strings.ToLower(strings.TrimSpace(out.ReplanLevel))
	switch out.Status {
	case "CHANGE_HARD":
		out.SetsPendingReplan = true
		out.ClearsPendingReplan = false
		out.ReplanLevel = "hard"
	case "ADAPT_SOFT":
		out.SetsPendingReplan = false
		out.ClearsPendingReplan = false
		out.ReplanLevel = "soft"
	case "NO_CHANGE":
		out.SetsPendingReplan = false
		out.ClearsPendingReplan = true
		out.ReplanLevel = "none"
	case "WAIT_NOISE":
		out.SetsPendingReplan = false
		out.ClearsPendingReplan = false
		out.ReplanLevel = "none"
	}
	if out.Confidence == 0 && out.Status != "WAIT_NOISE" {
		out.Confidence = 1
	}
	if out.Provider == "" {
		out.Provider = "llm-helper"
	}
	return out
}

func fallbackStudentTranslation(text, direction string) string {
	if sourceLanguageForDirection(direction) == "ru" {
		return "[translation unavailable] " + text
	}
	return "[перевод недоступен] " + text
}

func fallbackStudentAnswer(contextText, question string) string {
	if strings.TrimSpace(question) != "" {
		return "Пока LLM-сервис недоступен, но вопрос зафиксирован: " + strings.TrimSpace(question)
	}
	if strings.TrimSpace(contextText) != "" {
		return "TL;DR: Пока LLM-сервис недоступен, проверь последний фрагмент транскрипта и перевод выше.\nПример 1: Рассмотри последнюю финальную фразу в блоке «Оригинал» и её пару в блоке «Перевод»; если смысл расходится, задай уточняющий вопрос через «Спросить»."
	}
	return "TL;DR: Пока нет контекста для ответа.\nПример 1: Добавь первую фразу в оригинал или включи захват звука; после этого «Помоги» объяснит последний понятный фрагмент."
}
