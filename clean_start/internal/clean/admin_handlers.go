package clean

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
)

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
