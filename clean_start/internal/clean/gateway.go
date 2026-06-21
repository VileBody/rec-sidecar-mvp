package clean

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/nats-io/nats.go"
)

//go:embed web/index.html
var gatewayWeb embed.FS

type Gateway struct {
	cfg       Config
	nc        *nats.Conn
	inworld   *InworldClient
	soniox    *SonioxClient
	logger    *slog.Logger
	store     *Store
	authStore AuthStore
	tokens    TokenManager
	server    *http.Server
	sub       *nats.Subscription
	publish   func(*nats.Conn, Config, Event) error
}

func NewGateway(cfg Config, nc *nats.Conn, inworld *InworldClient, logger *slog.Logger) *Gateway {
	return &Gateway{
		cfg:     cfg,
		nc:      nc,
		inworld: inworld,
		soniox:  NewSonioxClient(cfg),
		logger:  logger.With("component", "gateway"),
		store:   NewStore(),
		tokens:  NewTokenManager(cfg.JWTSecret),
		publish: PublishEvent,
	}
}

func (g *Gateway) Run(ctx context.Context) error {
	if err := g.ensureAuth(ctx); err != nil {
		return err
	}
	subject := SubjectWildcard(g.cfg.SubjectPrefix)
	sub, err := g.nc.Subscribe(subject, func(msg *nats.Msg) {
		var event Event
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			g.logger.Warn("bad event payload", "subject", msg.Subject, "error", err)
			return
		}
		if event.SessionID == "" {
			if sessionID, _, ok := ParseSubject(g.cfg.SubjectPrefix, msg.Subject); ok {
				event.SessionID = sessionID
			}
		}
		g.store.Apply(event)
	})
	if err != nil {
		return err
	}
	g.sub = sub

	mux := http.NewServeMux()
	mux.HandleFunc("GET /", g.index)
	mux.HandleFunc("GET /healthz", g.healthz)
	mux.HandleFunc("POST /v1/auth/register", g.register)
	mux.HandleFunc("POST /v1/auth/login", g.login)
	mux.HandleFunc("GET /v1/auth/me", g.me)
	mux.HandleFunc("POST /v1/auth/logout", g.logout)
	mux.HandleFunc("POST /v1/sessions", g.createSession)
	mux.HandleFunc("GET /v1/sessions/{session_id}", g.getSession)
	mux.HandleFunc("POST /v1/sessions/{session_id}/events", g.postEvent)
	mux.HandleFunc("POST /v1/sessions/{session_id}/audio/log", g.logBrowserAudio)
	mux.HandleFunc("POST /v1/sessions/{session_id}/stt/transcribe", g.transcribePCM)
	mux.HandleFunc("GET /v1/sessions/{session_id}/stt/live", g.streamSTT)
	mux.HandleFunc("GET /v1/sessions/{session_id}/stream", g.streamSession)

	g.server = &http.Server{
		Addr:              g.cfg.HTTPAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		_ = g.Shutdown(shutdownCtx)
	}()

	g.logger.Info("gateway listening", "addr", g.cfg.HTTPAddr)
	err = g.server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (g *Gateway) Shutdown(ctx context.Context) error {
	if g.sub != nil {
		_ = g.sub.Unsubscribe()
	}
	if g.authStore != nil {
		_ = g.authStore.Close()
	}
	if g.server != nil {
		return g.server.Shutdown(ctx)
	}
	return nil
}

func (g *Gateway) emit(event Event) error {
	g.store.Apply(event)
	publish := g.publish
	if publish == nil {
		publish = PublishEvent
	}
	return publish(g.nc, g.cfg, event)
}

func (g *Gateway) ensureAuth(ctx context.Context) error {
	if !g.cfg.AuthEnabled {
		return nil
	}
	if !g.tokens.Configured() {
		return errors.New("CLEAN_START_JWT_SECRET is required when CLEAN_START_AUTH_ENABLED=true")
	}
	if g.authStore == nil {
		store, err := NewPostgresAuthStore(g.cfg.DatabaseURL)
		if err != nil {
			return err
		}
		g.authStore = store
	}
	return g.authStore.EnsureSchema(ctx)
}
