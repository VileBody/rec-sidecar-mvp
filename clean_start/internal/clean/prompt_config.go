package clean

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

type PromptConfig struct {
	ID        string    `json:"id"`
	UserType  string    `json:"user_type"`
	Kind      string    `json:"kind"`
	Key       string    `json:"key"`
	Title     string    `json:"title,omitempty"`
	Body      string    `json:"body"`
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	UpdatedBy string    `json:"updated_by,omitempty"`
}

type PromptConfigFilter struct {
	UserType string
	Kind     string
}

type UpsertPromptConfigRequest struct {
	Title   string `json:"title,omitempty"`
	Body    string `json:"body"`
	Enabled *bool  `json:"enabled,omitempty"`
}

type PromptConfigListResponse struct {
	PromptConfigs []PromptConfig `json:"prompt_configs"`
}

func normalizePromptUserType(userType string) (string, error) {
	userType = strings.ToLower(strings.TrimSpace(userType))
	switch userType {
	case UserRoleSales, UserRoleStudent:
		return userType, nil
	default:
		return "", errors.New("user_type must be sales or student")
	}
}

func normalizePromptKind(kind string) (string, error) {
	return normalizePromptIdentifier(kind, "kind")
}

func normalizePromptKey(key string) (string, error) {
	return normalizePromptIdentifier(key, "key")
}

func normalizePromptIdentifier(value, field string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return "", fmt.Errorf("%s is required", field)
	}
	if len(value) > 64 {
		return "", fmt.Errorf("%s must be at most 64 characters", field)
	}
	for _, ch := range value {
		if ch >= 'a' && ch <= 'z' {
			continue
		}
		if ch >= '0' && ch <= '9' {
			continue
		}
		if ch == '-' || ch == '_' || ch == '.' {
			continue
		}
		return "", fmt.Errorf("%s may contain only lowercase letters, numbers, dash, underscore, or dot", field)
	}
	return value, nil
}

func promptConfigStorageKey(userType, kind, key string) string {
	return userType + "\x00" + kind + "\x00" + key
}

func (s *memoryAuthStore) ListPromptConfigs(_ context.Context, filter PromptConfigFilter) ([]PromptConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	configs := make([]PromptConfig, 0, len(s.prompts))
	for _, config := range s.prompts {
		if filter.UserType != "" && config.UserType != filter.UserType {
			continue
		}
		if filter.Kind != "" && config.Kind != filter.Kind {
			continue
		}
		configs = append(configs, config)
	}
	sort.Slice(configs, func(i, j int) bool {
		left, right := configs[i], configs[j]
		if left.UserType != right.UserType {
			return left.UserType < right.UserType
		}
		if left.Kind != right.Kind {
			return left.Kind < right.Kind
		}
		return left.Key < right.Key
	})
	return configs, nil
}

func (s *memoryAuthStore) PromptConfig(_ context.Context, userType, kind, key string) (PromptConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	config, ok := s.prompts[promptConfigStorageKey(userType, kind, key)]
	if !ok {
		return PromptConfig{}, ErrAuthNotFound
	}
	return config, nil
}

func (s *memoryAuthStore) UpsertPromptConfig(_ context.Context, config PromptConfig) (PromptConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	storageKey := promptConfigStorageKey(config.UserType, config.Kind, config.Key)
	existing, ok := s.prompts[storageKey]
	if ok {
		config.ID = existing.ID
		config.CreatedAt = existing.CreatedAt
	} else {
		if config.ID == "" {
			config.ID = NewID("prompt")
		}
		config.CreatedAt = now
	}
	config.UpdatedAt = now
	s.prompts[storageKey] = config
	return config, nil
}

func (s *PostgresAuthStore) ListPromptConfigs(ctx context.Context, filter PromptConfigFilter) ([]PromptConfig, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT id, user_type, kind, config_key, title, body, enabled, created_at, updated_at, updated_by
		 FROM prompt_configs
		 WHERE ($1 = '' OR user_type = $1) AND ($2 = '' OR kind = $2)
		 ORDER BY user_type, kind, config_key`,
		filter.UserType,
		filter.Kind,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var configs []PromptConfig
	for rows.Next() {
		config, err := scanPromptConfig(rows)
		if err != nil {
			return nil, err
		}
		configs = append(configs, config)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return configs, nil
}

func (s *PostgresAuthStore) PromptConfig(ctx context.Context, userType, kind, key string) (PromptConfig, error) {
	return scanPromptConfig(s.db.QueryRowContext(
		ctx,
		`SELECT id, user_type, kind, config_key, title, body, enabled, created_at, updated_at, updated_by
		 FROM prompt_configs
		 WHERE user_type = $1 AND kind = $2 AND config_key = $3`,
		userType,
		kind,
		key,
	))
}

func (s *PostgresAuthStore) UpsertPromptConfig(ctx context.Context, config PromptConfig) (PromptConfig, error) {
	now := time.Now().UTC()
	if config.ID == "" {
		config.ID = NewID("prompt")
	}
	if config.CreatedAt.IsZero() {
		config.CreatedAt = now
	}
	config.UpdatedAt = now
	return scanPromptConfig(s.db.QueryRowContext(
		ctx,
		`INSERT INTO prompt_configs (id, user_type, kind, config_key, title, body, enabled, created_at, updated_at, updated_by)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
		 ON CONFLICT (user_type, kind, config_key) DO UPDATE
		 SET title = EXCLUDED.title,
		     body = EXCLUDED.body,
		     enabled = EXCLUDED.enabled,
		     updated_at = EXCLUDED.updated_at,
		     updated_by = EXCLUDED.updated_by
		 RETURNING id, user_type, kind, config_key, title, body, enabled, created_at, updated_at, updated_by`,
		config.ID,
		config.UserType,
		config.Kind,
		config.Key,
		config.Title,
		config.Body,
		config.Enabled,
		config.CreatedAt,
		config.UpdatedAt,
		config.UpdatedBy,
	))
}

type promptConfigScanner interface {
	Scan(dest ...any) error
}

func scanPromptConfig(scanner promptConfigScanner) (PromptConfig, error) {
	var config PromptConfig
	if err := scanner.Scan(
		&config.ID,
		&config.UserType,
		&config.Kind,
		&config.Key,
		&config.Title,
		&config.Body,
		&config.Enabled,
		&config.CreatedAt,
		&config.UpdatedAt,
		&config.UpdatedBy,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return PromptConfig{}, ErrAuthNotFound
		}
		return PromptConfig{}, err
	}
	return config, nil
}
