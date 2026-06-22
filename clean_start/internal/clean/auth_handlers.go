package clean

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"
)

func (g *Gateway) register(w http.ResponseWriter, r *http.Request) {
	if !g.authRequired() {
		writeError(w, http.StatusNotFound, errors.New("auth disabled"))
		return
	}
	var req RegisterRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	email, err := normalizeEmail(req.Email)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if err := validatePassword(req.Password); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	role, err := normalizePublicRegistrationRole(req.Role)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	passwordHash, err := hashPassword(req.Password)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	user, err := g.authStore.CreateUser(r.Context(), email, passwordHash, role)
	if err != nil {
		if errors.Is(err, ErrAuthConflict) {
			writeError(w, http.StatusConflict, errors.New("user already exists"))
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	response, err := g.issueAuthResponse(r.Context(), w, r, user)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusCreated, response)
}

func (g *Gateway) login(w http.ResponseWriter, r *http.Request) {
	if !g.authRequired() {
		writeError(w, http.StatusNotFound, errors.New("auth disabled"))
		return
	}
	var req LoginRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	email, err := normalizeEmail(req.Email)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	user, err := g.authStore.UserByEmail(r.Context(), email)
	if err != nil || !checkPassword(user.PasswordHash, req.Password) {
		writeError(w, http.StatusUnauthorized, errors.New("invalid email or password"))
		return
	}
	response, err := g.issueAuthResponse(r.Context(), w, r, user)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func (g *Gateway) me(w http.ResponseWriter, r *http.Request) {
	user, ok := g.requireUser(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"user": authUserResponse(user)})
}

func (g *Gateway) logout(w http.ResponseWriter, r *http.Request) {
	if !g.authRequired() {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	token := bearerToken(r)
	if token == "" {
		if cookie, err := r.Cookie(g.cfg.AuthCookieName); err == nil {
			token = cookie.Value
		}
	}
	if token != "" {
		if claims, err := g.tokens.Verify(token, time.Now()); err == nil {
			_ = g.authStore.RevokeAuthSession(r.Context(), claims.AuthSessionID)
		}
	}
	g.clearAuthCookie(w)
	w.WriteHeader(http.StatusNoContent)
}
