package clean

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type SonioxClient struct {
	cfg Config
}

type SonioxSTTStream struct {
	conn            *websocket.Conn
	mu              sync.Mutex
	seenFinal       map[string]struct{}
	finalTokens     []sonioxToken
	finalizePending bool
}

func NewSonioxClient(cfg Config) *SonioxClient {
	return &SonioxClient{cfg: cfg}
}

func (c *SonioxClient) Configured() bool {
	return strings.TrimSpace(c.cfg.SonioxAPIKey) != ""
}

func (c *SonioxClient) ConnectSTT(ctx context.Context) (*SonioxSTTStream, error) {
	return c.ConnectSTTWithLanguage(ctx, "")
}

func (c *SonioxClient) ConnectSTTWithLanguage(ctx context.Context, language string) (*SonioxSTTStream, error) {
	if !c.Configured() {
		return nil, errors.New("missing SONIOX_API_KEY")
	}
	dialer := websocket.Dialer{
		HandshakeTimeout: sttHandshakeTimeout,
		TLSClientConfig:  &tls.Config{MinVersion: tls.VersionTLS12},
	}
	conn, resp, err := dialer.DialContext(ctx, c.cfg.SonioxSTTWSURL, nil)
	if err != nil {
		status := ""
		if resp != nil {
			status = resp.Status
		}
		return nil, fmt.Errorf("soniox stt connect failed %s: %w", status, err)
	}

	config := map[string]any{
		"api_key":                        c.cfg.SonioxAPIKey,
		"model":                          c.cfg.SonioxSTTModel,
		"audio_format":                   c.cfg.SonioxAudioFormat,
		"sample_rate":                    audioSampleRate,
		"num_channels":                   audioChannels,
		"enable_speaker_diarization":     c.cfg.SonioxDiarize,
		"enable_language_identification": false,
		"enable_endpoint_detection":      c.cfg.SonioxEndpointDetection,
	}
	if language = strings.TrimSpace(language); language == "" {
		language = strings.TrimSpace(c.cfg.SonioxLanguage)
	}
	if language != "" {
		config["language_hints"] = []string{language}
		config["language_hints_strict"] = c.cfg.SonioxLanguageStrict
	}
	if c.cfg.SonioxEndpointDetection {
		config["endpoint_sensitivity"] = c.cfg.SonioxEndpointSensitivity
		config["max_endpoint_delay_ms"] = c.cfg.SonioxMaxEndpointDelayMS
	}
	if err := conn.WriteJSON(config); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return &SonioxSTTStream{conn: conn, seenFinal: make(map[string]struct{})}, nil
}

func (s *SonioxSTTStream) SendAudio(pcm []byte) error {
	const chunkBytes = audioSampleRate * 2 / 10
	for offset := 0; offset < len(pcm); offset += chunkBytes {
		end := offset + chunkBytes
		if end > len(pcm) {
			end = len(pcm)
		}
		if err := s.conn.WriteMessage(websocket.BinaryMessage, pcm[offset:end]); err != nil {
			return err
		}
	}
	return nil
}

func (s *SonioxSTTStream) SendEndTurn() error {
	s.mu.Lock()
	s.finalizePending = true
	s.mu.Unlock()
	return s.conn.WriteJSON(map[string]any{"type": "finalize"})
}

func (s *SonioxSTTStream) ReadTranscript() (STTTranscript, error) {
	_, raw, err := s.conn.ReadMessage()
	if err != nil {
		return STTTranscript{}, err
	}
	transcript, err := s.parseTranscript(raw)
	if err != nil {
		return STTTranscript{}, fmt.Errorf("soniox stt response: %w", err)
	}
	return transcript, nil
}

func (s *SonioxSTTStream) SetReadDeadline(deadline time.Time) error {
	return s.conn.SetReadDeadline(deadline)
}

func (s *SonioxSTTStream) Close() {
	// Soniox finalizes and closes cleanly on an empty text frame. In local tests,
	// an empty binary frame caused request_timeout even after audio was processed.
	_ = s.conn.WriteMessage(websocket.TextMessage, []byte{})
	_ = s.conn.Close()
}

func (s *SonioxSTTStream) parseTranscript(raw []byte) (STTTranscript, error) {
	var value map[string]any
	if err := json.Unmarshal(raw, &value); err != nil {
		return STTTranscript{}, nil
	}
	if code, ok := value["error_code"]; ok && code != nil {
		return STTTranscript{}, fmt.Errorf("%v", value["error_message"])
	}
	rawTokens, ok := value["tokens"].([]any)
	if !ok || len(rawTokens) == 0 {
		return STTTranscript{}, nil
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	var newFinalTokens []sonioxToken
	var nonFinalTokens []sonioxToken
	hasFin := false
	for _, item := range rawTokens {
		obj, ok := item.(map[string]any)
		if !ok {
			continue
		}
		token := sonioxTokenFromMap(obj)
		if token.Text == "" {
			continue
		}
		if token.Text == "<fin>" {
			hasFin = true
			continue
		}
		if token.Final {
			key := token.Key()
			if _, seen := s.seenFinal[key]; seen {
				continue
			}
			s.seenFinal[key] = struct{}{}
			s.finalTokens = append(s.finalTokens, token)
			newFinalTokens = append(newFinalTokens, token)
			continue
		}
		nonFinalTokens = append(nonFinalTokens, token)
	}

	if hasFin {
		transcript := sonioxTranscriptFromTokens(s.finalTokens, true)
		s.finalizePending = false
		s.finalTokens = nil
		return transcript, nil
	}
	if len(nonFinalTokens) > 0 {
		tokens := make([]sonioxToken, 0, len(s.finalTokens)+len(nonFinalTokens))
		tokens = append(tokens, s.finalTokens...)
		tokens = append(tokens, nonFinalTokens...)
		return sonioxTranscriptFromTokens(tokens, false), nil
	}
	if len(newFinalTokens) > 0 || (s.finalizePending && len(s.finalTokens) > 0) {
		return sonioxTranscriptFromTokens(s.finalTokens, false), nil
	}
	return STTTranscript{}, nil
}

type sonioxToken struct {
	Text    string
	Speaker string
	StartMS string
	EndMS   string
	Final   bool
}

func sonioxTokenFromMap(obj map[string]any) sonioxToken {
	text, _ := obj["text"].(string)
	return sonioxToken{
		Text:    text,
		Speaker: speakerID(obj["speaker"]),
		StartMS: sonioxNumberID(obj["start_ms"]),
		EndMS:   sonioxNumberID(obj["end_ms"]),
		Final:   boolValue(obj["is_final"]),
	}
}

func (t sonioxToken) Key() string {
	return t.StartMS + "|" + t.EndMS + "|" + t.Speaker + "|" + t.Text
}

func sonioxTranscriptFromTokens(tokens []sonioxToken, final bool) STTTranscript {
	var segments []STTSegment
	var currentSpeaker string
	var currentText strings.Builder
	flush := func() {
		text := strings.Join(strings.Fields(currentText.String()), " ")
		if text == "" {
			return
		}
		segments = append(segments, STTSegment{Speaker: currentSpeaker, Text: text})
		currentText.Reset()
	}
	for _, token := range tokens {
		if currentText.Len() > 0 && token.Speaker != currentSpeaker {
			flush()
		}
		currentSpeaker = token.Speaker
		currentText.WriteString(token.Text)
	}
	flush()

	var full strings.Builder
	for _, segment := range segments {
		if full.Len() > 0 {
			full.WriteString(" ")
		}
		full.WriteString(segment.Text)
	}
	return STTTranscript{Text: full.String(), Final: final, Segments: segments}
}

func sonioxNumberID(raw any) string {
	switch value := raw.(type) {
	case nil:
		return ""
	case string:
		return strings.TrimSpace(value)
	case float64:
		if value == float64(int64(value)) {
			return fmt.Sprintf("%d", int64(value))
		}
		return fmt.Sprintf("%g", value)
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func boolValue(raw any) bool {
	value, _ := raw.(bool)
	return value
}

var _ STTStream = (*SonioxSTTStream)(nil)
var _ STTStream = (*InworldSTTStream)(nil)
