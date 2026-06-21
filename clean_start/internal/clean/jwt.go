package clean

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

type TokenManager struct {
	secret []byte
}

type TokenClaims struct {
	UserID        string
	AuthSessionID string
	ExpiresAt     time.Time
}

func NewTokenManager(secret string) TokenManager {
	return TokenManager{secret: []byte(secret)}
}

func (m TokenManager) Configured() bool {
	return len(m.secret) > 0
}

func (m TokenManager) Sign(claims TokenClaims, now time.Time) (string, error) {
	if !m.Configured() {
		return "", errors.New("missing JWT secret")
	}
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	payload := map[string]any{
		"sub": claims.UserID,
		"sid": claims.AuthSessionID,
		"iat": now.Unix(),
		"exp": claims.ExpiresAt.Unix(),
	}
	headerRaw, err := json.Marshal(header)
	if err != nil {
		return "", err
	}
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	unsigned := base64.RawURLEncoding.EncodeToString(headerRaw) + "." + base64.RawURLEncoding.EncodeToString(payloadRaw)
	return unsigned + "." + m.signature(unsigned), nil
}

func (m TokenManager) Verify(token string, now time.Time) (TokenClaims, error) {
	if !m.Configured() {
		return TokenClaims{}, errors.New("missing JWT secret")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return TokenClaims{}, errors.New("invalid JWT")
	}
	unsigned := parts[0] + "." + parts[1]
	if !hmac.Equal([]byte(parts[2]), []byte(m.signature(unsigned))) {
		return TokenClaims{}, errors.New("invalid JWT signature")
	}
	headerRaw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return TokenClaims{}, errors.New("invalid JWT header")
	}
	var header struct {
		Alg string `json:"alg"`
		Typ string `json:"typ"`
	}
	if err := json.Unmarshal(headerRaw, &header); err != nil || header.Alg != "HS256" {
		return TokenClaims{}, errors.New("invalid JWT header")
	}
	payloadRaw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return TokenClaims{}, errors.New("invalid JWT payload")
	}
	var payload struct {
		Sub string `json:"sub"`
		SID string `json:"sid"`
		Exp int64  `json:"exp"`
	}
	if err := json.Unmarshal(payloadRaw, &payload); err != nil {
		return TokenClaims{}, errors.New("invalid JWT payload")
	}
	if payload.Sub == "" || payload.SID == "" || payload.Exp == 0 {
		return TokenClaims{}, errors.New("invalid JWT claims")
	}
	expiresAt := time.Unix(payload.Exp, 0).UTC()
	if !expiresAt.After(now) {
		return TokenClaims{}, errors.New("JWT expired")
	}
	return TokenClaims{UserID: payload.Sub, AuthSessionID: payload.SID, ExpiresAt: expiresAt}, nil
}

func (m TokenManager) signature(unsigned string) string {
	mac := hmac.New(sha256.New, m.secret)
	_, _ = mac.Write([]byte(unsigned))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func (c TokenClaims) String() string {
	return fmt.Sprintf("user=%s session=%s exp=%s", c.UserID, c.AuthSessionID, c.ExpiresAt.Format(time.RFC3339))
}
