package clean

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

type PostgresAuthStore struct {
	db *sql.DB
}

func NewPostgresAuthStore(databaseURL string) (*PostgresAuthStore, error) {
	if databaseURL == "" {
		return nil, errors.New("missing database URL")
	}
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	return &PostgresAuthStore{db: db}, nil
}

func (s *PostgresAuthStore) EnsureSchema(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS users (
			id TEXT PRIMARY KEY,
			email TEXT NOT NULL UNIQUE,
			role TEXT NOT NULL DEFAULT 'sales',
			password_hash TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL
		)`,
		`ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'sales'`,
		`CREATE TABLE IF NOT EXISTS auth_sessions (
			id TEXT PRIMARY KEY,
			user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL,
			expires_at TIMESTAMPTZ NOT NULL,
			revoked_at TIMESTAMPTZ NULL
		)`,
		`CREATE INDEX IF NOT EXISTS auth_sessions_user_id_idx ON auth_sessions(user_id)`,
		`CREATE TABLE IF NOT EXISTS app_sessions (
			id TEXT PRIMARY KEY,
			user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS app_sessions_user_id_idx ON app_sessions(user_id)`,
		`CREATE TABLE IF NOT EXISTS app_events (
			id TEXT PRIMARY KEY,
			session_id TEXT NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
			type TEXT NOT NULL,
			source TEXT NOT NULL,
			generation_id TEXT NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL,
			data JSONB NULL
		)`,
		`CREATE INDEX IF NOT EXISTS app_events_session_created_idx ON app_events(session_id, created_at, id)`,
	}
	for _, statement := range statements {
		if _, err := s.db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func (s *PostgresAuthStore) CreateUser(ctx context.Context, email, passwordHash, role string) (User, error) {
	user := User{ID: NewID("usr"), Email: email, Role: normalizeUserRoleOrDefault(role), PasswordHash: passwordHash, CreatedAt: time.Now().UTC()}
	_, err := s.db.ExecContext(ctx, `INSERT INTO users (id, email, role, password_hash, created_at) VALUES ($1, $2, $3, $4, $5)`, user.ID, user.Email, user.Role, user.PasswordHash, user.CreatedAt)
	if err != nil {
		if isUniqueViolation(err) {
			return User{}, ErrAuthConflict
		}
		return User{}, err
	}
	return user, nil
}

func (s *PostgresAuthStore) UserByEmail(ctx context.Context, email string) (User, error) {
	return s.scanUser(s.db.QueryRowContext(ctx, `SELECT id, email, role, password_hash, created_at FROM users WHERE email = $1`, email))
}

func (s *PostgresAuthStore) UserByID(ctx context.Context, userID string) (User, error) {
	return s.scanUser(s.db.QueryRowContext(ctx, `SELECT id, email, role, password_hash, created_at FROM users WHERE id = $1`, userID))
}

func (s *PostgresAuthStore) scanUser(row *sql.Row) (User, error) {
	var user User
	if err := row.Scan(&user.ID, &user.Email, &user.Role, &user.PasswordHash, &user.CreatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return User{}, ErrAuthNotFound
		}
		return User{}, err
	}
	return user, nil
}

func (s *PostgresAuthStore) CreateAuthSession(ctx context.Context, session AuthSession) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO auth_sessions (id, user_id, created_at, expires_at, revoked_at) VALUES ($1, $2, $3, $4, $5)`, session.ID, session.UserID, session.CreatedAt, session.ExpiresAt, session.RevokedAt)
	return err
}

func (s *PostgresAuthStore) AuthSessionByID(ctx context.Context, sessionID string) (AuthSession, error) {
	var session AuthSession
	if err := s.db.QueryRowContext(ctx, `SELECT id, user_id, created_at, expires_at, revoked_at FROM auth_sessions WHERE id = $1`, sessionID).Scan(&session.ID, &session.UserID, &session.CreatedAt, &session.ExpiresAt, &session.RevokedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return AuthSession{}, ErrAuthNotFound
		}
		return AuthSession{}, err
	}
	return session, nil
}

func (s *PostgresAuthStore) RevokeAuthSession(ctx context.Context, sessionID string) error {
	result, err := s.db.ExecContext(ctx, `UPDATE auth_sessions SET revoked_at = $2 WHERE id = $1 AND revoked_at IS NULL`, sessionID, time.Now().UTC())
	if err != nil {
		return err
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		return ErrAuthNotFound
	}
	return nil
}

func (s *PostgresAuthStore) CreateAppSession(ctx context.Context, sessionID, userID string) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO app_sessions (id, user_id, created_at) VALUES ($1, $2, $3)`, sessionID, userID, time.Now().UTC())
	if err != nil {
		if isUniqueViolation(err) {
			return ErrAuthConflict
		}
		return err
	}
	return nil
}

func (s *PostgresAuthStore) AppSessionOwner(ctx context.Context, sessionID string) (string, error) {
	var userID string
	if err := s.db.QueryRowContext(ctx, `SELECT user_id FROM app_sessions WHERE id = $1`, sessionID).Scan(&userID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", ErrAuthNotFound
		}
		return "", err
	}
	return userID, nil
}

func (s *PostgresAuthStore) LatestAppSession(ctx context.Context, userID string) (string, error) {
	var sessionID string
	if err := s.db.QueryRowContext(ctx, `SELECT id FROM app_sessions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1`, userID).Scan(&sessionID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", ErrAuthNotFound
		}
		return "", err
	}
	return sessionID, nil
}

func (s *PostgresAuthStore) SaveAppEvent(ctx context.Context, event Event) error {
	var data any
	if len(event.Data) > 0 {
		data = string(event.Data)
	}
	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO app_events (id, session_id, type, source, generation_id, created_at, data)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (id) DO NOTHING`,
		event.ID,
		event.SessionID,
		event.Type,
		event.Source,
		event.GenerationID,
		event.CreatedAt,
		data,
	)
	return err
}

func (s *PostgresAuthStore) AppEvents(ctx context.Context, sessionID string) ([]Event, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, session_id, type, source, generation_id, created_at, data FROM app_events WHERE session_id = $1 ORDER BY created_at, id`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []Event
	for rows.Next() {
		var event Event
		var data sql.NullString
		if err := rows.Scan(&event.ID, &event.SessionID, &event.Type, &event.Source, &event.GenerationID, &event.CreatedAt, &data); err != nil {
			return nil, err
		}
		if data.Valid && data.String != "" {
			event.Data = append([]byte(nil), data.String...)
		}
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return events, nil
}

func (s *PostgresAuthStore) Close() error {
	return s.db.Close()
}

func isUniqueViolation(err error) bool {
	return err != nil && (strings.Contains(err.Error(), "SQLSTATE 23505") || strings.Contains(err.Error(), "duplicate key"))
}

func (s *PostgresAuthStore) String() string {
	return fmt.Sprintf("postgres-auth-store")
}
