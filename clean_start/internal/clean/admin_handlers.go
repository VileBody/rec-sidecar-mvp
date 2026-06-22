package clean

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
)

type AdminSessionSummary struct {
	SessionID       string    `json:"session_id"`
	UserID          string    `json:"user_id"`
	UserEmail       string    `json:"user_email"`
	UserRole        string    `json:"user_role"`
	CreatedAt       time.Time `json:"created_at"`
	LastEventAt     time.Time `json:"last_event_at"`
	DurationSeconds int64     `json:"duration_seconds"`
	EventCount      int64     `json:"event_count"`
	TranscriptCount int64     `json:"transcript_count"`
}

type AdminSessionListResponse struct {
	Items []AdminSessionSummary `json:"items"`
}

type AdminSessionDetailResponse struct {
	Summary    AdminSessionSummary `json:"summary"`
	State      SessionState        `json:"state"`
	Transcript []TranscriptItem    `json:"transcript"`
	Events     []Event             `json:"events"`
}

type AdminPromptItem struct {
	ID        string `json:"id"`
	UserType  string `json:"user_type"`
	Kind      string `json:"kind"`
	Key       string `json:"key"`
	Title     string `json:"title,omitempty"`
	Content   string `json:"content"`
	Enabled   bool   `json:"enabled"`
	CreatedAt string `json:"created_at,omitempty"`
	UpdatedAt string `json:"updated_at,omitempty"`
	UpdatedBy string `json:"updated_by,omitempty"`
}

type AdminPromptListResponse struct {
	Items []AdminPromptItem `json:"items"`
}

type UpsertAdminPromptRequest struct {
	UserType string `json:"user_type"`
	Kind     string `json:"kind,omitempty"`
	Key      string `json:"key"`
	Title    string `json:"title,omitempty"`
	Content  string `json:"content"`
	Enabled  *bool  `json:"enabled,omitempty"`
}

func (g *Gateway) requireAdmin(w http.ResponseWriter, r *http.Request) (User, bool) {
	user, ok := g.requireUser(w, r)
	if !ok {
		return User{}, false
	}
	if normalizeUserRoleOrDefault(user.Role) != UserRoleAdmin {
		writeError(w, http.StatusForbidden, errors.New("admin role required"))
		return User{}, false
	}
	return user, true
}

func (g *Gateway) listAdminPrompts(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r); !ok {
		return
	}
	filter := PromptConfigFilter{}
	if raw := strings.TrimSpace(r.URL.Query().Get("user_type")); raw != "" {
		userType, err := normalizePromptUserType(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		filter.UserType = userType
	}
	if raw := strings.TrimSpace(r.URL.Query().Get("kind")); raw != "" {
		kind, err := normalizePromptKind(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		filter.Kind = kind
	}
	configs, err := g.authStore.ListPromptConfigs(r.Context(), filter)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	items := make([]AdminPromptItem, 0, len(configs))
	for _, config := range configs {
		items = append(items, adminPromptItem(config))
	}
	writeJSON(w, http.StatusOK, AdminPromptListResponse{Items: items})
}

func (g *Gateway) listAdminSessions(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r); !ok {
		return
	}
	limit := 200
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed <= 0 {
			writeError(w, http.StatusBadRequest, errors.New("limit must be a positive integer"))
			return
		}
		limit = parsed
	}
	items, err := g.authStore.ListAppSessionSummaries(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, AdminSessionListResponse{Items: items})
}

func (g *Gateway) getAdminSession(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r); !ok {
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		writeError(w, http.StatusBadRequest, errors.New("session_id is required"))
		return
	}
	summary, err := g.authStore.AppSessionSummary(r.Context(), sessionID)
	if err != nil {
		if errors.Is(err, ErrAuthNotFound) {
			writeError(w, http.StatusNotFound, errors.New("session not found"))
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	events, err := g.authStore.AppEvents(r.Context(), sessionID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	state := stateFromEvents(sessionID, events)
	writeJSON(w, http.StatusOK, AdminSessionDetailResponse{
		Summary:    summary,
		State:      state,
		Transcript: state.Transcript,
		Events:     events,
	})
}

func (g *Gateway) upsertAdminPrompt(w http.ResponseWriter, r *http.Request) {
	admin, ok := g.requireAdmin(w, r)
	if !ok {
		return
	}
	var req UpsertAdminPromptRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	userType, err := normalizePromptUserType(req.UserType)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	kind := req.Kind
	if strings.TrimSpace(kind) == "" {
		kind = "prompt"
	}
	kind, err = normalizePromptKind(kind)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	key := req.Key
	if strings.TrimSpace(key) == "" {
		key = r.PathValue("prompt_id")
	}
	key, err = normalizePromptKey(key)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if strings.TrimSpace(req.Content) == "" {
		writeError(w, http.StatusBadRequest, errors.New("content is required"))
		return
	}
	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	config, err := g.authStore.UpsertPromptConfig(r.Context(), PromptConfig{
		UserType:  userType,
		Kind:      kind,
		Key:       key,
		Title:     strings.TrimSpace(req.Title),
		Body:      req.Content,
		Enabled:   enabled,
		UpdatedBy: admin.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"item": adminPromptItem(config)})
}

func (g *Gateway) listPromptConfigs(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r); !ok {
		return
	}
	filter := PromptConfigFilter{}
	if raw := strings.TrimSpace(r.URL.Query().Get("user_type")); raw != "" {
		userType, err := normalizePromptUserType(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		filter.UserType = userType
	}
	if raw := strings.TrimSpace(r.URL.Query().Get("kind")); raw != "" {
		kind, err := normalizePromptKind(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		filter.Kind = kind
	}
	configs, err := g.authStore.ListPromptConfigs(r.Context(), filter)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, PromptConfigListResponse{PromptConfigs: configs})
}

func (g *Gateway) getPromptConfig(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r); !ok {
		return
	}
	userType, kind, key, ok := promptConfigPathValues(w, r)
	if !ok {
		return
	}
	config, err := g.authStore.PromptConfig(r.Context(), userType, kind, key)
	if err != nil {
		if errors.Is(err, ErrAuthNotFound) {
			writeError(w, http.StatusNotFound, errors.New("prompt config not found"))
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, config)
}

func (g *Gateway) upsertPromptConfig(w http.ResponseWriter, r *http.Request) {
	admin, ok := g.requireAdmin(w, r)
	if !ok {
		return
	}
	userType, kind, key, ok := promptConfigPathValues(w, r)
	if !ok {
		return
	}
	var req UpsertPromptConfigRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	body := strings.TrimSpace(req.Body)
	if body == "" {
		writeError(w, http.StatusBadRequest, errors.New("body is required"))
		return
	}
	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	config, err := g.authStore.UpsertPromptConfig(r.Context(), PromptConfig{
		UserType:  userType,
		Kind:      kind,
		Key:       key,
		Title:     strings.TrimSpace(req.Title),
		Body:      body,
		Enabled:   enabled,
		UpdatedBy: admin.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, config)
}

func promptConfigPathValues(w http.ResponseWriter, r *http.Request) (string, string, string, bool) {
	userType, err := normalizePromptUserType(r.PathValue("user_type"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return "", "", "", false
	}
	kind, err := normalizePromptKind(r.PathValue("kind"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return "", "", "", false
	}
	key, err := normalizePromptKey(r.PathValue("key"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return "", "", "", false
	}
	return userType, kind, key, true
}

func adminPromptItem(config PromptConfig) AdminPromptItem {
	return AdminPromptItem{
		ID:        config.ID,
		UserType:  config.UserType,
		Kind:      config.Kind,
		Key:       config.Key,
		Title:     config.Title,
		Content:   config.Body,
		Enabled:   config.Enabled,
		CreatedAt: config.CreatedAt.Format(time.RFC3339Nano),
		UpdatedAt: config.UpdatedAt.Format(time.RFC3339Nano),
		UpdatedBy: config.UpdatedBy,
	}
}

func stateFromEvents(sessionID string, events []Event) SessionState {
	store := NewStore()
	for _, event := range events {
		if event.SessionID == "" {
			event.SessionID = sessionID
		}
		store.Apply(event)
	}
	state, ok := store.Get(sessionID)
	if ok {
		return state
	}
	return SessionState{SessionID: sessionID}
}

func sortAdminSessionSummaries(items []AdminSessionSummary) {
	sort.Slice(items, func(i, j int) bool {
		left := items[i].LastEventAt
		right := items[j].LastEventAt
		if left.Equal(right) {
			return items[i].CreatedAt.After(items[j].CreatedAt)
		}
		return left.After(right)
	})
}

func durationSeconds(start, end time.Time) int64 {
	if start.IsZero() || end.IsZero() || end.Before(start) {
		return 0
	}
	return int64(end.Sub(start).Seconds())
}

func isTranscriptLikeEvent(eventType string) bool {
	switch eventType {
	case EventSTTFinal, EventSellerInput, EventClientFinal, EventStudentInput:
		return true
	default:
		return false
	}
}

func appSessionSummaryQuery(where string) string {
	limitParam := "$1"
	if strings.TrimSpace(where) != "" {
		limitParam = "$2"
	}
	return `
		WITH event_stats AS (
			SELECT
				session_id,
				MAX(created_at) AS last_event_at,
				COUNT(*) AS event_count,
				COUNT(*) FILTER (
					WHERE type IN ('` + EventSTTFinal + `', '` + EventSellerInput + `', '` + EventClientFinal + `', '` + EventStudentInput + `')
				) AS transcript_count
			FROM app_events
			GROUP BY session_id
		)
		SELECT
			s.id,
			s.user_id,
			u.email,
			u.role,
			s.created_at,
			COALESCE(es.last_event_at, s.created_at) AS last_event_at,
			EXTRACT(EPOCH FROM (COALESCE(es.last_event_at, s.created_at) - s.created_at))::BIGINT AS duration_seconds,
			COALESCE(es.event_count, 0)::BIGINT AS event_count,
			COALESCE(es.transcript_count, 0)::BIGINT AS transcript_count
		FROM app_sessions s
		JOIN users u ON u.id = s.user_id
		LEFT JOIN event_stats es ON es.session_id = s.id
		` + where + `
		ORDER BY COALESCE(es.last_event_at, s.created_at) DESC, s.created_at DESC
		LIMIT ` + limitParam
}

type adminSessionSummaryScanner interface {
	Scan(dest ...any) error
}

func scanAdminSessionSummary(scanner adminSessionSummaryScanner) (AdminSessionSummary, error) {
	var summary AdminSessionSummary
	if err := scanner.Scan(
		&summary.SessionID,
		&summary.UserID,
		&summary.UserEmail,
		&summary.UserRole,
		&summary.CreatedAt,
		&summary.LastEventAt,
		&summary.DurationSeconds,
		&summary.EventCount,
		&summary.TranscriptCount,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return AdminSessionSummary{}, ErrAuthNotFound
		}
		return AdminSessionSummary{}, err
	}
	return summary, nil
}
