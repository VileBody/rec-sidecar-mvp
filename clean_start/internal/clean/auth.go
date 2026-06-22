package clean

import (
	"context"
	"errors"
	"net/http"
	"net/mail"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
)

var (
	ErrAuthNotFound = errors.New("auth record not found")
	ErrAuthConflict = errors.New("auth record conflict")
)

const (
	UserRoleSales   = "sales"
	UserRoleStudent = "student"
	UserRoleAdmin   = "admin"
)

type User struct {
	ID           string    `json:"id"`
	Email        string    `json:"email"`
	Role         string    `json:"role"`
	PasswordHash string    `json:"-"`
	CreatedAt    time.Time `json:"created_at"`
}

type AuthSession struct {
	ID        string     `json:"id"`
	UserID    string     `json:"user_id"`
	CreatedAt time.Time  `json:"created_at"`
	ExpiresAt time.Time  `json:"expires_at"`
	RevokedAt *time.Time `json:"revoked_at,omitempty"`
}

type AuthStore interface {
	EnsureSchema(context.Context) error
	CreateUser(ctx context.Context, email, passwordHash, role string) (User, error)
	UserByEmail(ctx context.Context, email string) (User, error)
	UserByID(ctx context.Context, userID string) (User, error)
	CreateAuthSession(ctx context.Context, session AuthSession) error
	AuthSessionByID(ctx context.Context, sessionID string) (AuthSession, error)
	RevokeAuthSession(ctx context.Context, sessionID string) error
	CreateAppSession(ctx context.Context, sessionID, userID string) error
	AppSessionOwner(ctx context.Context, sessionID string) (string, error)
	LatestAppSession(ctx context.Context, userID string) (string, error)
	SaveAppEvent(ctx context.Context, event Event) error
	AppEvents(ctx context.Context, sessionID string) ([]Event, error)
	ListAppSessionSummaries(ctx context.Context, limit int) ([]AdminSessionSummary, error)
	AppSessionSummary(ctx context.Context, sessionID string) (AdminSessionSummary, error)
	ListPromptConfigs(ctx context.Context, filter PromptConfigFilter) ([]PromptConfig, error)
	PromptConfig(ctx context.Context, userType, kind, key string) (PromptConfig, error)
	UpsertPromptConfig(ctx context.Context, config PromptConfig) (PromptConfig, error)
	Close() error
}

type RegisterRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Role     string `json:"role,omitempty"`
}

type LoginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type AuthResponse struct {
	User      AuthUserResponse `json:"user"`
	Token     string           `json:"token"`
	ExpiresAt time.Time        `json:"expires_at"`
}

type AuthUserResponse struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Role  string `json:"role"`
}

func authUserResponse(user User) AuthUserResponse {
	role := normalizeUserRoleOrDefault(user.Role)
	return AuthUserResponse{ID: user.ID, Email: user.Email, Role: role}
}

func normalizeEmail(email string) (string, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	if email == "" {
		return "", errors.New("email is required")
	}
	if _, err := mail.ParseAddress(email); err != nil {
		return "", errors.New("invalid email")
	}
	return email, nil
}

func validatePassword(password string) error {
	if len([]rune(password)) < 8 {
		return errors.New("password must be at least 8 characters")
	}
	return nil
}

func normalizeUserRole(role string) (string, error) {
	role = strings.ToLower(strings.TrimSpace(role))
	if role == "" {
		return UserRoleSales, nil
	}
	switch role {
	case UserRoleSales, UserRoleStudent, UserRoleAdmin:
		return role, nil
	default:
		return "", errors.New("role must be sales, student, or admin")
	}
}

func normalizePublicRegistrationRole(role string) (string, error) {
	role = strings.ToLower(strings.TrimSpace(role))
	if role == "" {
		return UserRoleSales, nil
	}
	switch role {
	case UserRoleSales, UserRoleStudent:
		return role, nil
	default:
		return "", errors.New("role must be sales or student")
	}
}

func normalizeUserRoleOrDefault(role string) string {
	normalized, err := normalizeUserRole(role)
	if err != nil {
		return UserRoleSales
	}
	return normalized
}

func hashPassword(password string) (string, error) {
	raw, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	return string(raw), err
}

func checkPassword(hash, password string) bool {
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) == nil
}

func (g *Gateway) authRequired() bool {
	return g.cfg.AuthEnabled
}

func (g *Gateway) requireUser(w http.ResponseWriter, r *http.Request) (User, bool) {
	if !g.authRequired() {
		return User{ID: "dev", Email: "dev@local", Role: UserRoleSales}, true
	}
	user, err := g.currentUser(r)
	if err != nil {
		writeError(w, http.StatusUnauthorized, err)
		return User{}, false
	}
	return user, true
}

func (g *Gateway) currentUser(r *http.Request) (User, error) {
	token := bearerToken(r)
	if token == "" {
		if cookie, err := r.Cookie(g.cfg.AuthCookieName); err == nil {
			token = cookie.Value
		}
	}
	if token == "" {
		return User{}, errors.New("missing auth token")
	}
	claims, err := g.tokens.Verify(token, time.Now())
	if err != nil {
		return User{}, err
	}
	session, err := g.authStore.AuthSessionByID(r.Context(), claims.AuthSessionID)
	if err != nil {
		return User{}, errors.New("invalid auth session")
	}
	now := time.Now()
	if session.UserID != claims.UserID || session.RevokedAt != nil || !session.ExpiresAt.After(now) {
		return User{}, errors.New("invalid auth session")
	}
	return g.authStore.UserByID(r.Context(), claims.UserID)
}

func bearerToken(r *http.Request) string {
	value := strings.TrimSpace(r.Header.Get("Authorization"))
	if value == "" {
		return ""
	}
	const prefix = "Bearer "
	if !strings.HasPrefix(value, prefix) {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(value, prefix))
}

func (g *Gateway) requireSessionOwner(w http.ResponseWriter, r *http.Request, sessionID string) (User, bool) {
	user, ok := g.requireUser(w, r)
	if !ok {
		return User{}, false
	}
	if !g.authRequired() {
		return user, true
	}
	ownerID, err := g.authStore.AppSessionOwner(r.Context(), sessionID)
	if err != nil {
		if errors.Is(err, ErrAuthNotFound) {
			writeError(w, http.StatusNotFound, errors.New("session not found"))
			return User{}, false
		}
		writeError(w, http.StatusInternalServerError, err)
		return User{}, false
	}
	if ownerID != user.ID {
		writeError(w, http.StatusForbidden, errors.New("session belongs to another user"))
		return User{}, false
	}
	return user, true
}

func (g *Gateway) setAuthCookie(w http.ResponseWriter, r *http.Request, token string, expiresAt time.Time) {
	http.SetCookie(w, &http.Cookie{
		Name:     g.cfg.AuthCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   g.cfg.AuthCookieSecure,
		Expires:  expiresAt,
	})
}

func (g *Gateway) clearAuthCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     g.cfg.AuthCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   g.cfg.AuthCookieSecure,
		MaxAge:   -1,
	})
}

func (g *Gateway) issueAuthResponse(ctx context.Context, w http.ResponseWriter, r *http.Request, user User) (AuthResponse, error) {
	now := time.Now().UTC()
	expiresAt := now.Add(g.cfg.JWTTTL)
	authSession := AuthSession{
		ID:        NewID("auth"),
		UserID:    user.ID,
		CreatedAt: now,
		ExpiresAt: expiresAt,
	}
	if err := g.authStore.CreateAuthSession(ctx, authSession); err != nil {
		return AuthResponse{}, err
	}
	token, err := g.tokens.Sign(TokenClaims{UserID: user.ID, AuthSessionID: authSession.ID, ExpiresAt: expiresAt}, now)
	if err != nil {
		return AuthResponse{}, err
	}
	g.setAuthCookie(w, r, token, expiresAt)
	return AuthResponse{User: authUserResponse(user), Token: token, ExpiresAt: expiresAt}, nil
}

type memoryAuthStore struct {
	mu           sync.Mutex
	usersByID    map[string]User
	usersByEmail map[string]string
	authSessions map[string]AuthSession
	appSessions  map[string]string
	appCreatedAt map[string]time.Time
	appEvents    map[string][]Event
	prompts      map[string]PromptConfig
}

func NewMemoryAuthStore() AuthStore {
	return &memoryAuthStore{
		usersByID:    make(map[string]User),
		usersByEmail: make(map[string]string),
		authSessions: make(map[string]AuthSession),
		appSessions:  make(map[string]string),
		appCreatedAt: make(map[string]time.Time),
		appEvents:    make(map[string][]Event),
		prompts:      make(map[string]PromptConfig),
	}
}

func (s *memoryAuthStore) EnsureSchema(context.Context) error { return nil }

func (s *memoryAuthStore) CreateUser(_ context.Context, email, passwordHash, role string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.usersByEmail[email]; ok {
		return User{}, ErrAuthConflict
	}
	user := User{ID: NewID("usr"), Email: email, Role: normalizeUserRoleOrDefault(role), PasswordHash: passwordHash, CreatedAt: time.Now().UTC()}
	s.usersByID[user.ID] = user
	s.usersByEmail[email] = user.ID
	return user, nil
}

func (s *memoryAuthStore) UserByEmail(_ context.Context, email string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	id, ok := s.usersByEmail[email]
	if !ok {
		return User{}, ErrAuthNotFound
	}
	return s.usersByID[id], nil
}

func (s *memoryAuthStore) UserByID(_ context.Context, userID string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	user, ok := s.usersByID[userID]
	if !ok {
		return User{}, ErrAuthNotFound
	}
	return user, nil
}

func (s *memoryAuthStore) CreateAuthSession(_ context.Context, session AuthSession) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.authSessions[session.ID] = session
	return nil
}

func (s *memoryAuthStore) AuthSessionByID(_ context.Context, sessionID string) (AuthSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	session, ok := s.authSessions[sessionID]
	if !ok {
		return AuthSession{}, ErrAuthNotFound
	}
	return session, nil
}

func (s *memoryAuthStore) RevokeAuthSession(_ context.Context, sessionID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	session, ok := s.authSessions[sessionID]
	if !ok {
		return ErrAuthNotFound
	}
	now := time.Now().UTC()
	session.RevokedAt = &now
	s.authSessions[sessionID] = session
	return nil
}

func (s *memoryAuthStore) CreateAppSession(_ context.Context, sessionID, userID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.appSessions[sessionID] = userID
	s.appCreatedAt[sessionID] = time.Now().UTC()
	return nil
}

func (s *memoryAuthStore) AppSessionOwner(_ context.Context, sessionID string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	userID, ok := s.appSessions[sessionID]
	if !ok {
		return "", ErrAuthNotFound
	}
	return userID, nil
}

func (s *memoryAuthStore) LatestAppSession(_ context.Context, userID string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var latestID string
	var latestTime time.Time
	for sessionID, ownerID := range s.appSessions {
		if ownerID != userID {
			continue
		}
		createdAt := s.appCreatedAt[sessionID]
		if latestID == "" || createdAt.After(latestTime) {
			latestID = sessionID
			latestTime = createdAt
		}
	}
	if latestID == "" {
		return "", ErrAuthNotFound
	}
	return latestID, nil
}

func (s *memoryAuthStore) SaveAppEvent(_ context.Context, event Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, existing := range s.appEvents[event.SessionID] {
		if existing.ID == event.ID {
			return nil
		}
	}
	s.appEvents[event.SessionID] = append(s.appEvents[event.SessionID], event)
	return nil
}

func (s *memoryAuthStore) AppEvents(_ context.Context, sessionID string) ([]Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	events := append([]Event(nil), s.appEvents[sessionID]...)
	return events, nil
}

func (s *memoryAuthStore) ListAppSessionSummaries(_ context.Context, limit int) ([]AdminSessionSummary, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if limit <= 0 || limit > 500 {
		limit = 200
	}
	summaries := make([]AdminSessionSummary, 0, len(s.appSessions))
	for sessionID := range s.appSessions {
		summaries = append(summaries, s.appSessionSummaryLocked(sessionID))
	}
	sortAdminSessionSummaries(summaries)
	if len(summaries) > limit {
		summaries = summaries[:limit]
	}
	return summaries, nil
}

func (s *memoryAuthStore) AppSessionSummary(_ context.Context, sessionID string) (AdminSessionSummary, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.appSessions[sessionID]; !ok {
		return AdminSessionSummary{}, ErrAuthNotFound
	}
	return s.appSessionSummaryLocked(sessionID), nil
}

func (s *memoryAuthStore) appSessionSummaryLocked(sessionID string) AdminSessionSummary {
	userID := s.appSessions[sessionID]
	user := s.usersByID[userID]
	createdAt := s.appCreatedAt[sessionID]
	lastEventAt := createdAt
	eventCount := int64(0)
	transcriptCount := int64(0)
	for _, event := range s.appEvents[sessionID] {
		eventCount++
		if event.CreatedAt.After(lastEventAt) {
			lastEventAt = event.CreatedAt
		}
		if isTranscriptLikeEvent(event.Type) {
			transcriptCount++
		}
	}
	return AdminSessionSummary{
		SessionID:       sessionID,
		UserID:          userID,
		UserEmail:       user.Email,
		UserRole:        normalizeUserRoleOrDefault(user.Role),
		CreatedAt:       createdAt,
		LastEventAt:     lastEventAt,
		DurationSeconds: durationSeconds(createdAt, lastEventAt),
		EventCount:      eventCount,
		TranscriptCount: transcriptCount,
	}
}

func (s *memoryAuthStore) Close() error { return nil }
