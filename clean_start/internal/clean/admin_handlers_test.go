package clean

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func issueTestAuthUser(t *testing.T, g *Gateway, email, role string) AuthResponse {
	t.Helper()
	normalizedEmail, err := normalizeEmail(email)
	if err != nil {
		t.Fatal(err)
	}
	passwordHash, err := hashPassword("password123")
	if err != nil {
		t.Fatal(err)
	}
	user, err := g.authStore.CreateUser(context.Background(), normalizedEmail, passwordHash, role)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/auth/test-token", nil)
	rec := httptest.NewRecorder()
	response, err := g.issueAuthResponse(context.Background(), rec, req, user)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func TestAdminPromptConfigsRequireAdmin(t *testing.T) {
	g := newAuthTestGateway()

	req := httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs", nil)
	rec := httptest.NewRecorder()
	g.listPromptConfigs(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauth status = %d body=%s", rec.Code, rec.Body.String())
	}

	sales := registerTestUser(t, g, "sales-admin-test@example.com")
	req = httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs", nil)
	req.Header.Set("Authorization", "Bearer "+sales.Token)
	rec = httptest.NewRecorder()
	g.listPromptConfigs(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("sales list status = %d body=%s", rec.Code, rec.Body.String())
	}

	admin := issueTestAuthUser(t, g, "admin-list@example.com", UserRoleAdmin)
	req = httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs", nil)
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.listPromptConfigs(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin list status = %d body=%s", rec.Code, rec.Body.String())
	}
	var response PromptConfigListResponse
	if err := json.NewDecoder(rec.Body).Decode(&response); err != nil {
		t.Fatal(err)
	}
	if len(response.PromptConfigs) != 0 {
		t.Fatalf("prompt configs = %#v, want empty", response.PromptConfigs)
	}
}

func TestAdminPromptConfigUpsertGetAndList(t *testing.T) {
	g := newAuthTestGateway()
	admin := issueTestAuthUser(t, g, "admin-prompts@example.com", UserRoleAdmin)

	req := httptest.NewRequest(
		http.MethodPut,
		"/v1/admin/prompt-configs/sales/playbook/default",
		strings.NewReader(`{"title":"Default Sales","body":"Coach sellers toward the next best action.","enabled":false}`),
	)
	setPromptConfigPath(req, "sales", "playbook", "default")
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec := httptest.NewRecorder()
	g.upsertPromptConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("upsert status = %d body=%s", rec.Code, rec.Body.String())
	}
	created := authJSON[PromptConfig](t, rec)
	if created.ID == "" || created.UserType != UserRoleSales || created.Kind != "playbook" || created.Key != "default" {
		t.Fatalf("unexpected created config: %#v", created)
	}
	if created.Title != "Default Sales" || created.Body != "Coach sellers toward the next best action." || created.Enabled {
		t.Fatalf("unexpected created payload: %#v", created)
	}
	if created.UpdatedBy != admin.User.ID {
		t.Fatalf("updated_by = %q, want %q", created.UpdatedBy, admin.User.ID)
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs/sales/playbook/default", nil)
	setPromptConfigPath(req, "sales", "playbook", "default")
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.getPromptConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status = %d body=%s", rec.Code, rec.Body.String())
	}
	got := authJSON[PromptConfig](t, rec)
	if got.ID != created.ID || got.Body != created.Body || got.Enabled != created.Enabled {
		t.Fatalf("got config = %#v, want %#v", got, created)
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs?user_type=sales&kind=playbook", nil)
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.listPromptConfigs(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d body=%s", rec.Code, rec.Body.String())
	}
	listed := authJSON[PromptConfigListResponse](t, rec)
	if len(listed.PromptConfigs) != 1 || listed.PromptConfigs[0].ID != created.ID {
		t.Fatalf("listed configs = %#v", listed.PromptConfigs)
	}

	req = httptest.NewRequest(
		http.MethodPut,
		"/v1/admin/prompt-configs/sales/playbook/default",
		strings.NewReader(`{"body":"Updated playbook body.","enabled":true}`),
	)
	setPromptConfigPath(req, "sales", "playbook", "default")
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.upsertPromptConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("second upsert status = %d body=%s", rec.Code, rec.Body.String())
	}
	updated := authJSON[PromptConfig](t, rec)
	if updated.ID != created.ID || updated.Body != "Updated playbook body." || !updated.Enabled {
		t.Fatalf("updated config = %#v", updated)
	}
}

func TestAdminPromptConfigForbiddenForSalesUser(t *testing.T) {
	g := newAuthTestGateway()
	sales := registerTestUser(t, g, "sales-forbidden@example.com")

	req := httptest.NewRequest(
		http.MethodPut,
		"/v1/admin/prompt-configs/sales/playbook/default",
		strings.NewReader(`{"body":"should not write"}`),
	)
	setPromptConfigPath(req, "sales", "playbook", "default")
	req.Header.Set("Authorization", "Bearer "+sales.Token)
	rec := httptest.NewRecorder()
	g.upsertPromptConfig(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("sales upsert status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestAdminPromptsUIContract(t *testing.T) {
	g := newAuthTestGateway()
	admin := issueTestAuthUser(t, g, "admin-ui-prompts@example.com", UserRoleAdmin)

	req := httptest.NewRequest(
		http.MethodPut,
		"/v1/admin/prompts/default_sales",
		strings.NewReader(`{"user_type":"sales","kind":"playbook","key":"default_sales","title":"Sales Playbook","content":"Открыть рамку и уточнить контекст."}`),
	)
	req.SetPathValue("prompt_id", "default_sales")
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec := httptest.NewRecorder()
	g.upsertAdminPrompt(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("ui upsert status = %d body=%s", rec.Code, rec.Body.String())
	}
	var saved struct {
		Item AdminPromptItem `json:"item"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&saved); err != nil {
		t.Fatal(err)
	}
	if saved.Item.UserType != UserRoleSales || saved.Item.Kind != "playbook" || saved.Item.Key != "default_sales" || saved.Item.Content == "" {
		t.Fatalf("unexpected saved item: %#v", saved.Item)
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/prompts?user_type=sales", nil)
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.listAdminPrompts(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("ui list status = %d body=%s", rec.Code, rec.Body.String())
	}
	var listed AdminPromptListResponse
	if err := json.NewDecoder(rec.Body).Decode(&listed); err != nil {
		t.Fatal(err)
	}
	if len(listed.Items) != 1 || listed.Items[0].ID != saved.Item.ID || listed.Items[0].Content != saved.Item.Content {
		t.Fatalf("listed items = %#v, saved=%#v", listed.Items, saved.Item)
	}
}

func TestAdminSessionsListAndDetail(t *testing.T) {
	g := newAuthTestGateway()
	sales := registerTestUser(t, g, "admin-session-sales@example.com")
	admin := issueTestAuthUser(t, g, "admin-session-admin@example.com", UserRoleAdmin)

	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", strings.NewReader(`{"auto_opener":false}`))
	req.Header.Set("Authorization", "Bearer "+sales.Token)
	rec := httptest.NewRecorder()
	g.createSession(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create session status = %d body=%s", rec.Code, rec.Body.String())
	}
	session := authJSON[CreateSessionResponse](t, rec)

	req = httptest.NewRequest(http.MethodPost, "/v1/sessions/"+session.SessionID+"/events", strings.NewReader(`{"type":"stt.final","role":"student_original","text":"Привет, что происходит?","source":"test","speaker":"1"}`))
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+sales.Token)
	rec = httptest.NewRecorder()
	g.postEvent(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("post stt status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/sessions", nil)
	req.Header.Set("Authorization", "Bearer "+sales.Token)
	rec = httptest.NewRecorder()
	g.listAdminSessions(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("sales list sessions status = %d body=%s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/sessions", nil)
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.listAdminSessions(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin list sessions status = %d body=%s", rec.Code, rec.Body.String())
	}
	listed := authJSON[AdminSessionListResponse](t, rec)
	if len(listed.Items) != 1 {
		t.Fatalf("sessions len = %d, want 1: %#v", len(listed.Items), listed.Items)
	}
	if listed.Items[0].SessionID != session.SessionID || listed.Items[0].UserEmail != sales.User.Email || listed.Items[0].TranscriptCount != 1 || listed.Items[0].EventCount < 2 {
		t.Fatalf("unexpected session summary: %#v", listed.Items[0])
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/admin/sessions/"+session.SessionID, nil)
	req.SetPathValue("session_id", session.SessionID)
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec = httptest.NewRecorder()
	g.getAdminSession(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin detail status = %d body=%s", rec.Code, rec.Body.String())
	}
	detail := authJSON[AdminSessionDetailResponse](t, rec)
	if detail.Summary.SessionID != session.SessionID || len(detail.Events) < 2 {
		t.Fatalf("unexpected detail summary/events: %#v", detail)
	}
	if len(detail.Transcript) != 1 || detail.Transcript[0].Text != "Привет, что происходит?" {
		t.Fatalf("unexpected transcript: %#v", detail.Transcript)
	}
}

func TestAdminPromptConfigMissingReturnsNotFound(t *testing.T) {
	g := newAuthTestGateway()
	admin := issueTestAuthUser(t, g, "admin-missing@example.com", UserRoleAdmin)

	req := httptest.NewRequest(http.MethodGet, "/v1/admin/prompt-configs/student/prompt/help", nil)
	setPromptConfigPath(req, "student", "prompt", "help")
	req.Header.Set("Authorization", "Bearer "+admin.Token)
	rec := httptest.NewRecorder()
	g.getPromptConfig(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing status = %d body=%s", rec.Code, rec.Body.String())
	}
}

func setPromptConfigPath(req *http.Request, userType, kind, key string) {
	req.SetPathValue("user_type", userType)
	req.SetPathValue("kind", kind)
	req.SetPathValue("key", key)
}
