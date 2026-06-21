package clean

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"
)

func (g *Gateway) streamSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if _, ok := g.requireSessionOwner(w, r, sessionID); !ok {
		return
	}
	if _, ok := g.store.Get(sessionID); !ok {
		writeError(w, http.StatusNotFound, fmt.Errorf("session %s not found", sessionID))
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, errors.New("streaming unsupported"))
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	events, cancel := g.store.Subscribe(sessionID)
	defer cancel()

	if state, ok := g.store.Get(sessionID); ok {
		writeSSE(w, "snapshot", state)
		flusher.Flush()
	}

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			fmt.Fprint(w, ": keepalive\n\n")
			flusher.Flush()
		case event := <-events:
			writeSSE(w, "event", event)
			if state, ok := g.store.Get(sessionID); ok {
				writeSSE(w, "snapshot", state)
			}
			flusher.Flush()
		}
	}
}

func writeSSE(w http.ResponseWriter, event string, value any) {
	raw, _ := json.Marshal(value)
	fmt.Fprintf(w, "event: %s\n", event)
	fmt.Fprintf(w, "data: %s\n\n", raw)
}
