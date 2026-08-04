package clean

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.opentelemetry.io/otel/attribute"
)

var sttWSUpgrader = websocket.Upgrader{
	ReadBufferSize:  64 * 1024,
	WriteBufferSize: 64 * 1024,
}

type STTTranscribeRequest struct {
	Role      string `json:"role,omitempty"`
	Source    string `json:"source,omitempty"`
	Language  string `json:"language,omitempty"`
	Direction string `json:"direction,omitempty"`
	PCMBase64 string `json:"pcm_base64"`
}

type STTTranscribeResponse struct {
	Text string `json:"text"`
	Role string `json:"role"`
}

type BrowserSTTStreamMessage struct {
	AudioChunk  *AudioChunkMessage `json:"audio_chunk,omitempty"`
	EndTurn     map[string]any     `json:"end_turn,omitempty"`
	CloseStream map[string]any     `json:"close_stream,omitempty"`
}

type AudioChunkMessage struct {
	Content string `json:"content"`
}

func (g *Gateway) transcribePCM(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	user, ok := g.requireSessionOwner(w, r, sessionID)
	if !ok {
		return
	}
	accountRole := normalizeUserRoleOrDefault(user.Role)
	ctx, span := StartSpan(r.Context(), "stt.transcribe_pcm", attribute.String("session.id_hash", shortHash(sessionID)))
	var spanErr error
	defer func() { EndSpan(span, spanErr) }()
	var req STTTranscribeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(req.PCMBase64))
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("bad pcm_base64: %w", err))
		return
	}
	role := strings.TrimSpace(req.Role)
	roleReason := "explicit:" + role
	if role == "" {
		role = "client"
		roleReason = "default:client"
	}
	source := normalizeCaptureSource(req.Source)
	if sourceRole, ok := roleForCaptureSource(source); ok {
		role = sourceRole
		roleReason = "source:" + source
	}
	language := strings.TrimSpace(req.Language)
	direction := strings.TrimSpace(req.Direction)
	if language == "" && direction != "" {
		language = sourceLanguageForDirection(direction)
	}
	if g.audioSink != nil {
		g.audioSink.RecordPCMAsync(sessionID, role, source, raw)
	}
	started := time.Now()
	stream, provider, err := g.connectSTTWithLanguage(ctx, language)
	if err != nil {
		spanErr = err
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	span.SetAttributes(attribute.String("stt.provider", provider), attribute.String("role", role), attribute.String("source", source))
	defer stream.Close()
	g.logger.Info("browser audio stt received", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw))
	text, err := transcribePCMWithStream(stream, provider, raw)
	elapsedMS := time.Since(started).Milliseconds()
	if err != nil {
		if errors.Is(err, ErrNoSpeech) {
			g.logger.Info("browser audio stt no speech", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw), "elapsed_ms", elapsedMS)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		g.logger.Warn("browser audio stt failed", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw), "elapsed_ms", elapsedMS, "error", err)
		ObserveHistogram("seller_stt_partial_latency_ms", float64(elapsedMS), map[string]string{"provider": provider, "source": source, "role": role, "status": "error"})
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if reason := browserTranscriptRejectReasonForAccountRole(text, role, accountRole); reason != "" {
		g.logger.Info("browser audio stt rejected", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw), "elapsed_ms", elapsedMS, "reason", reason, "text", text)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if reason := g.crossSourceEchoRejectReason(sessionID, role, source, text); reason != "" {
		g.logger.Info("browser audio stt rejected", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw), "elapsed_ms", elapsedMS, "reason", reason, "text", text)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	g.logger.Info("browser audio stt final", "session_id", sessionID, "role", role, "source", source, "provider", provider, "bytes", len(raw), "elapsed_ms", elapsedMS, "text_len", len([]rune(text)), "text", text)
	ObserveHistogram("seller_stt_partial_latency_ms", float64(elapsedMS), map[string]string{"provider": provider, "source": source, "role": role, "status": "ok"})
	IncCounter("seller_stt_final_total", map[string]string{"provider": provider, "source": source, "role": role})
	event := NewEventFromContext(ctx, sessionID, EventSTTFinal, "gateway-stt", SpeechData{
		Role:        role,
		RoleReason:  roleReason,
		AccountRole: accountRole,
		Text:        text,
		Source:      source,
		Direction:   direction,
		Language:    language,
	})
	if err := g.emit(event); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, STTTranscribeResponse{Text: text, Role: role})
}

func (g *Gateway) streamSTT(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	user, ok := g.requireSessionOwner(w, r, sessionID)
	if !ok {
		return
	}
	accountRole := normalizeUserRoleOrDefault(user.Role)
	ctx, span := StartSpan(r.Context(), "stt.websocket_stream", attribute.String("session.id_hash", shortHash(sessionID)))
	var spanErr error
	defer func() { EndSpan(span, spanErr) }()
	role := strings.TrimSpace(r.URL.Query().Get("role"))
	if role == "" {
		role = "client"
	}
	if role == "mixed" || role == "diarized" {
		role = "mixed"
	}
	source := normalizeCaptureSource(r.URL.Query().Get("source"))
	if source == "" {
		source = CaptureSourceMixedAudio
	}
	direction := strings.TrimSpace(r.URL.Query().Get("direction"))
	language := strings.TrimSpace(r.URL.Query().Get("language"))
	if language == "" && direction != "" {
		language = sourceLanguageForDirection(direction)
	}
	speakerRoles := speakerRolesFromQuery(r.URL.Query())
	stabilizer := newSTTStreamStabilizer()
	segmentTracker := newSTTSegmentTracker()

	browserConn, err := sttWSUpgrader.Upgrade(w, r, nil)
	if err != nil {
		g.logger.Warn("browser stt ws upgrade failed", "session_id", sessionID, "role", role, "source", source, "error", err)
		return
	}
	defer browserConn.Close()

	stream, provider, err := g.connectSTTWithLanguage(ctx, language)
	if err != nil {
		g.logger.Warn("browser stt provider connect failed", "session_id", sessionID, "role", role, "source", source, "provider", provider, "error", err)
		_ = browserConn.WriteJSON(map[string]any{
			"type":      "error",
			"error":     err.Error(),
			"retryable": sttProviderErrorRetryable(err),
		})
		spanErr = err
		return
	}
	span.SetAttributes(
		attribute.String("stt.provider", provider),
		attribute.String("source", source),
		attribute.String("role", role),
		attribute.String("language", language),
	)
	defer stream.Close()
	unregisterMicStream := g.registerMicStream(sessionID, source)
	defer unregisterMicStream()
	recorder := (*AudioRecorder)(nil)
	if g.audioSink != nil {
		recorder = g.audioSink.Start(sessionID, role, source)
		defer func() {
			if recorder == nil {
				return
			}
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
				defer cancel()
				if err := recorder.Close(ctx); err != nil {
					g.logger.Warn("audio recording upload failed", "session_id", sessionID, "role", role, "source", source, "error", err)
				}
			}()
		}()
	}

	var browserWriteMu sync.Mutex
	writeBrowserJSON := func(value any) error {
		browserWriteMu.Lock()
		defer browserWriteMu.Unlock()
		return browserConn.WriteJSON(value)
	}
	writeBrowserReject := func(segment STTSegment, segmentID, segmentRole, roleReason, reason string, score float64, final bool) error {
		payload := map[string]any{
			"type":        "stt.rejected",
			"text":        segment.Text,
			"final":       final,
			"role":        segmentRole,
			"role_reason": roleReason,
			"source":      source,
			"speaker":     segment.Speaker,
			"segment_id":  segmentID,
			"reason":      reason,
			"created_at":  time.Now().UTC().Format(time.RFC3339Nano),
		}
		if score > 0 {
			payload["echo_score"] = score
		}
		return writeBrowserJSON(payload)
	}

	_ = writeBrowserJSON(map[string]any{"type": "ready"})
	g.logger.Info("browser audio stt stream connected", "session_id", sessionID, "role", role, "source", source, "provider", provider, "speaker_roles", speakerRoles)

	done := make(chan error, 2)
	audioCommands := make(chan sttAudioCommand, 128)
	audioStats := &sttAudioStats{}
	go func() {
		for {
			transcript, err := stream.ReadTranscript()
			if err != nil {
				done <- err
				return
			}
			if transcript.Text == "" {
				continue
			}
			eventType := EventSTTPartial
			if transcript.Final {
				eventType = EventSTTFinal
			}
			for index, segment := range diarizedTranscriptSegments(transcript) {
				segmentRole, roleReason := roleReasonForSTTSource(role, source, segment.Speaker, speakerRoles)
				segmentID := segmentTracker.ID(segment, index)
				if !stabilizer.ShouldEmit(segmentID, segment.Text, transcript.Final) {
					continue
				}
				if suppressSystemSellerSegment(g.hasActiveMicStream(sessionID), source, segmentRole) {
					g.logger.Info("browser audio stt stream rejected", "session_id", sessionID, "role", segmentRole, "role_reason", roleReason, "source", source, "speaker", segment.Speaker, "reason", "system_seller_suppressed_by_active_mic", "text", segment.Text)
					IncCounter("seller_text_echo_rejected_total", map[string]string{"reason": "system_seller_suppressed_by_active_mic", "source": source, "role": segmentRole})
					if err := writeBrowserReject(segment, segmentID, segmentRole, roleReason, "system_seller_suppressed_by_active_mic", 1, transcript.Final); err != nil {
						done <- err
						return
					}
					continue
				}
				if reason := browserTranscriptRejectReasonForAccountRole(segment.Text, segmentRole, accountRole); reason != "" {
					g.logger.Info("browser audio stt stream rejected", "session_id", sessionID, "role", segmentRole, "role_reason", roleReason, "source", source, "speaker", segment.Speaker, "reason", reason, "text", segment.Text)
					IncCounter("seller_text_echo_rejected_total", map[string]string{"reason": reason, "source": source, "role": segmentRole})
					if err := writeBrowserReject(segment, segmentID, segmentRole, roleReason, reason, 0, transcript.Final); err != nil {
						done <- err
						return
					}
					continue
				}
				if segmentRole == "client" || segmentRole == "seller" {
					if match := g.crossSourceEchoRejectMatch(sessionID, segmentRole, source, segment.Text); match.Found() {
						g.logger.Info("browser audio stt stream rejected", "session_id", sessionID, "role", segmentRole, "role_reason", roleReason, "source", source, "speaker", segment.Speaker, "reason", match.Reason, "echo_score", match.Score, "text", segment.Text)
						IncCounter("seller_text_echo_rejected_total", map[string]string{"reason": match.Reason, "source": source, "role": segmentRole})
						if err := writeBrowserReject(segment, segmentID, segmentRole, roleReason, match.Reason, match.Score, transcript.Final); err != nil {
							done <- err
							return
						}
						continue
					}
				}
				event := NewEventFromContext(ctx, sessionID, eventType, "gateway-stt-live", SpeechData{
					Role:        segmentRole,
					RoleReason:  roleReason,
					AccountRole: accountRole,
					Text:        segment.Text,
					Source:      source,
					Speaker:     segment.Speaker,
					SegmentID:   segmentID,
					Direction:   direction,
					Language:    language,
				})
				if err := g.emit(event); err != nil {
					done <- err
					return
				}
				if transcript.Final {
					IncCounter("seller_stt_final_total", map[string]string{"provider": provider, "source": source, "role": segmentRole})
				} else {
					IncCounter("seller_stt_partial_total", map[string]string{"provider": provider, "source": source, "role": segmentRole})
				}
				g.logger.Info("browser audio stt stream transcript", "session_id", sessionID, "role", segmentRole, "role_reason", roleReason, "source", source, "speaker", segment.Speaker, "final", transcript.Final, "created_at", event.CreatedAt.Format(time.RFC3339Nano), "text_len", len([]rune(segment.Text)), "text", segment.Text)
				if err := writeBrowserJSON(map[string]any{
					"type":        eventType,
					"text":        segment.Text,
					"final":       transcript.Final,
					"role":        segmentRole,
					"role_reason": roleReason,
					"speaker":     segment.Speaker,
					"segment_id":  segmentID,
					"created_at":  event.CreatedAt.Format(time.RFC3339Nano),
				}); err != nil {
					done <- err
					return
				}
			}
			if transcript.Final {
				segmentTracker.NextTurn()
			}
		}
	}()

	go func() {
		err := runSTTAudioJitterBuffer(stream, audioCommands, audioStats)
		if err != nil {
			done <- err
			return
		}
		done <- nil
	}()

	go func() {
		defer close(audioCommands)
		lastAudioLogAt := time.Now()
		for {
			var msg BrowserSTTStreamMessage
			if err := browserConn.ReadJSON(&msg); err != nil {
				g.logger.Info("browser audio stt stream reader closed", "session_id", sessionID, "role", role, "source", source, "audio_chunks", audioStats.audioChunks.Load(), "audio_bytes", audioStats.audioBytes.Load(), "audio_flushes", audioStats.audioFlushes.Load(), "audio_flushed_bytes", audioStats.audioFlushedBytes.Load(), "queue_half_flushes", audioStats.audioQueueFlushes.Load(), "dropped_queue_commands", audioStats.audioDroppedQueueCommands.Load(), "dropped_queue_bytes", audioStats.audioDroppedQueueBytes.Load(), "end_turns", audioStats.endTurns.Load(), "error", err)
				halfFlushSTTAudioCommandQueue(audioCommands, audioStats)
				audioCommands <- sttAudioCommand{err: err}
				return
			}
			if msg.CloseStream != nil {
				g.logger.Info("browser audio stt stream close requested", "session_id", sessionID, "role", role, "source", source, "audio_chunks", audioStats.audioChunks.Load(), "audio_bytes", audioStats.audioBytes.Load(), "audio_flushes", audioStats.audioFlushes.Load(), "audio_flushed_bytes", audioStats.audioFlushedBytes.Load(), "queue_half_flushes", audioStats.audioQueueFlushes.Load(), "dropped_queue_commands", audioStats.audioDroppedQueueCommands.Load(), "dropped_queue_bytes", audioStats.audioDroppedQueueBytes.Load(), "end_turns", audioStats.endTurns.Load())
				halfFlushSTTAudioCommandQueue(audioCommands, audioStats)
				audioCommands <- sttAudioCommand{close: true}
				return
			}
			if msg.EndTurn != nil {
				halfFlushSTTAudioCommandQueue(audioCommands, audioStats)
				audioCommands <- sttAudioCommand{endTurn: true}
				continue
			}
			if msg.AudioChunk == nil {
				continue
			}
			raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(msg.AudioChunk.Content))
			if err != nil {
				done <- fmt.Errorf("bad ws audio chunk: %w", err)
				return
			}
			if len(raw) == 0 {
				continue
			}
			if recorder != nil {
				if err := recorder.WritePCM(raw); err != nil {
					g.logger.Warn("audio recording write failed", "session_id", sessionID, "role", role, "source", source, "error", err)
				}
			}
			audioStats.audioChunks.Add(1)
			audioStats.audioBytes.Add(int64(len(raw)))
			if now := time.Now(); now.Sub(lastAudioLogAt) >= 5*time.Second {
				lastAudioLogAt = now
				g.logger.Info(
					"browser audio stt stream audio",
					"session_id", sessionID,
					"role", role,
					"source", source,
					"audio_chunks", audioStats.audioChunks.Load(),
					"audio_bytes", audioStats.audioBytes.Load(),
					"audio_flushes", audioStats.audioFlushes.Load(),
					"audio_flushed_bytes", audioStats.audioFlushedBytes.Load(),
					"queued_commands", len(audioCommands),
					"queue_half_flushes", audioStats.audioQueueFlushes.Load(),
					"dropped_queue_commands", audioStats.audioDroppedQueueCommands.Load(),
					"dropped_queue_bytes", audioStats.audioDroppedQueueBytes.Load(),
					"end_turns", audioStats.endTurns.Load(),
					"coalesced_end_turns", audioStats.coalescedEndTurns.Load(),
					"max_buffer_bytes", audioStats.maxBufferBytes.Load(),
				)
			}
			enqueueSTTAudioCommand(audioCommands, sttAudioCommand{audio: raw}, audioStats)
		}
	}()

	err = <-done
	if err != nil && !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway, websocket.CloseNoStatusReceived) {
		g.logger.Warn("browser audio stt stream closed", "session_id", sessionID, "role", role, "source", source, "error", err)
		_ = writeBrowserJSON(map[string]any{
			"type":      "error",
			"error":     err.Error(),
			"retryable": sttProviderErrorRetryable(err),
		})
		return
	}
	g.logger.Info("browser audio stt stream closed", "session_id", sessionID, "role", role, "source", source)
}

func sttProviderErrorRetryable(err error) bool {
	if err == nil {
		return true
	}
	message := strings.ToLower(err.Error())
	for _, marker := range []string{
		"balance exhausted",
		"insufficient balance",
		"insufficient credits",
		"payment required",
		"invalid api key",
		"invalid api_key",
	} {
		if strings.Contains(message, marker) {
			return false
		}
	}
	return true
}

func (g *Gateway) sttStatus() (string, bool) {
	provider, configured := g.selectedSTTProvider()
	return provider, configured
}

func (g *Gateway) connectSTT(ctx context.Context) (STTStream, string, error) {
	return g.connectSTTWithLanguage(ctx, "")
}

func (g *Gateway) connectSTTWithLanguage(ctx context.Context, language string) (STTStream, string, error) {
	provider, configured := g.selectedSTTProvider()
	if !configured {
		return nil, provider, errors.New("missing STT provider config: set SONIOX_API_KEY or INWORLD_API_KEY")
	}
	switch provider {
	case "soniox":
		stream, err := g.soniox.ConnectSTTWithLanguage(ctx, language)
		return stream, provider, err
	case "inworld":
		stream, err := g.inworld.ConnectSTTWithLanguage(ctx, language)
		return stream, provider, err
	default:
		return nil, provider, fmt.Errorf("unsupported STT provider %q", provider)
	}
}

func (g *Gateway) selectedSTTProvider() (string, bool) {
	provider := strings.ToLower(strings.TrimSpace(g.cfg.STTProvider))
	switch provider {
	case "soniox":
		return "soniox", g.soniox != nil && g.soniox.Configured()
	case "inworld":
		return "inworld", g.inworld != nil && g.inworld.Configured()
	default:
		if g.soniox != nil && g.soniox.Configured() {
			return "soniox", true
		}
		if g.inworld != nil && g.inworld.Configured() {
			return "inworld", true
		}
		return "auto", false
	}
}
