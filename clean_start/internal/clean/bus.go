package clean

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
)

func ConnectNATS(cfg Config, logger *slog.Logger) (*nats.Conn, error) {
	nc, err := nats.Connect(
		cfg.NATSURL,
		nats.Name("clean-start-"+cfg.Role),
		nats.Timeout(8*time.Second),
		nats.ReconnectWait(500*time.Millisecond),
		nats.MaxReconnects(-1),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			logger.Warn("nats disconnected", "error", err)
		}),
		nats.ReconnectHandler(func(conn *nats.Conn) {
			logger.Info("nats reconnected", "url", conn.ConnectedUrl())
		}),
	)
	if err != nil {
		return nil, err
	}
	logger.Info("nats connected", "url", nc.ConnectedUrl(), "role", cfg.Role)
	return nc, nil
}

func PublishEvent(nc *nats.Conn, cfg Config, event Event) error {
	payload, err := json.Marshal(event)
	if err != nil {
		return err
	}
	subject := Subject(cfg.SubjectPrefix, event.SessionID, event.Type)
	msg := nats.NewMsg(subject)
	msg.Data = payload
	if event.TraceParent != "" {
		msg.Header.Set("traceparent", event.TraceParent)
	}
	if event.TraceID != "" {
		msg.Header.Set("x-trace-id", event.TraceID)
	}
	if err := nc.PublishMsg(msg); err != nil {
		return fmt.Errorf("publish %s: %w", subject, err)
	}
	return nil
}
