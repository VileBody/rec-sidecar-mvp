package clean

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

const (
	audioSampleRate = 16000
	audioChannels   = 1
	audioBitDepth   = 16
)

var ErrNoSpeech = errors.New("inworld stt returned no speech")

type InworldClient struct {
	cfg    Config
	client *http.Client
}

type InworldSTTStream struct {
	conn *websocket.Conn
}

type AudioResult struct {
	PCM  []byte
	WAV  []byte
	MIME string
}

func NewInworldClient(cfg Config) *InworldClient {
	return &InworldClient{
		cfg: cfg,
		client: &http.Client{
			Timeout: 45 * time.Second,
		},
	}
}

func (c *InworldClient) Configured() bool {
	return strings.TrimSpace(c.cfg.InworldAPIKey) != ""
}

func (c *InworldClient) Synthesize(ctx context.Context, speaker, text string) (AudioResult, error) {
	if !c.Configured() {
		return AudioResult{}, errors.New("missing INWORLD_API_KEY")
	}
	voice := c.cfg.InworldClientVoice
	if strings.EqualFold(speaker, "seller") {
		voice = c.cfg.InworldSellerVoice
	}
	payload := map[string]any{
		"text":    text,
		"voiceId": voice,
		"modelId": c.cfg.InworldTTSModel,
		"audioConfig": map[string]any{
			"audioEncoding":   "LINEAR16",
			"sampleRateHertz": audioSampleRate,
			"language":        c.cfg.InworldLanguage,
		},
	}
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.InworldTTSBase+"/tts/v1/voice", bytes.NewReader(raw))
	if err != nil {
		return AudioResult{}, err
	}
	req.Header.Set("Authorization", inworldAuthorization(c.cfg.InworldAPIKey))
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return AudioResult{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return AudioResult{}, fmt.Errorf("inworld tts http %d: %s", resp.StatusCode, string(body))
	}
	var value struct {
		AudioContent    string `json:"audioContent"`
		AudioContentAlt string `json:"audio_content"`
	}
	if err := json.Unmarshal(body, &value); err != nil {
		return AudioResult{}, err
	}
	audioContent := value.AudioContent
	if audioContent == "" {
		audioContent = value.AudioContentAlt
	}
	if audioContent == "" {
		return AudioResult{}, fmt.Errorf("inworld tts returned no audioContent")
	}
	audio, err := base64.StdEncoding.DecodeString(audioContent)
	if err != nil {
		return AudioResult{}, err
	}
	if bytes.HasPrefix(audio, []byte("RIFF")) {
		pcm, err := wavToPCM(audio)
		if err != nil {
			return AudioResult{}, err
		}
		return AudioResult{PCM: pcm, WAV: audio, MIME: "audio/wav"}, nil
	}
	return AudioResult{PCM: audio, WAV: pcmToWAV(audio), MIME: "audio/wav"}, nil
}

func (c *InworldClient) TranscribePCM(ctx context.Context, pcm []byte) (string, error) {
	if !c.Configured() {
		return "", errors.New("missing INWORLD_API_KEY")
	}
	if len(pcm) == 0 {
		return "", errors.New("empty pcm")
	}
	stream, err := c.ConnectSTT(ctx)
	if err != nil {
		return "", err
	}
	defer stream.Close()

	if err := stream.SendAudio(pcm); err != nil {
		return "", err
	}
	if err := stream.SendEndTurn(); err != nil {
		return "", fmt.Errorf("inworld stt end_turn: %w", err)
	}

	_ = stream.conn.SetReadDeadline(time.Now().Add(8 * time.Second))
	var lastPartial string
	for {
		text, final, err := stream.ReadTranscript()
		if err != nil {
			if lastPartial != "" {
				return lastPartial, nil
			}
			if netErr, ok := err.(interface{ Timeout() bool }); ok && netErr.Timeout() {
				return "", ErrNoSpeech
			}
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				return "", ErrNoSpeech
			}
			return "", fmt.Errorf("inworld stt read: %w", err)
		}
		if text == "" {
			continue
		}
		if final {
			return text, nil
		}
		lastPartial = text
	}
}

func (c *InworldClient) ConnectSTT(ctx context.Context) (*InworldSTTStream, error) {
	if !c.Configured() {
		return nil, errors.New("missing INWORLD_API_KEY")
	}
	header := http.Header{}
	header.Set("Authorization", inworldAuthorization(c.cfg.InworldAPIKey))
	dialer := websocket.Dialer{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	conn, resp, err := dialer.DialContext(ctx, c.cfg.InworldSTTWSURL, header)
	if err != nil {
		status := ""
		if resp != nil {
			status = resp.Status
		}
		return nil, fmt.Errorf("inworld stt connect failed %s: %w", status, err)
	}

	config := map[string]any{
		"transcribe_config": map[string]any{
			"modelId":          c.cfg.InworldSTTModel,
			"audioEncoding":    "LINEAR16",
			"sampleRateHertz":  audioSampleRate,
			"numberOfChannels": audioChannels,
		},
	}
	transcribeConfig := config["transcribe_config"].(map[string]any)
	if language := strings.TrimSpace(c.cfg.InworldSTTLanguage); language != "" {
		transcribeConfig["language"] = language
	} else {
		transcribeConfig["enableLanguageDetection"] = true
	}
	if err := conn.WriteJSON(config); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return &InworldSTTStream{conn: conn}, nil
}

func (s *InworldSTTStream) SendAudio(pcm []byte) error {
	const chunkBytes = audioSampleRate * 2 / 10
	for offset := 0; offset < len(pcm); offset += chunkBytes {
		end := offset + chunkBytes
		if end > len(pcm) {
			end = len(pcm)
		}
		chunk := pcm[offset:end]
		if len(chunk) < 640 {
			padded := make([]byte, 640)
			copy(padded, chunk)
			chunk = padded
		}
		if err := s.conn.WriteJSON(map[string]any{
			"audio_chunk": map[string]any{
				"content": base64.StdEncoding.EncodeToString(chunk),
			},
		}); err != nil {
			return err
		}
	}
	return nil
}

func (s *InworldSTTStream) SendEndTurn() error {
	return s.conn.WriteJSON(map[string]any{"end_turn": map[string]any{}})
}

func (s *InworldSTTStream) ReadTranscript() (string, bool, error) {
	_, raw, err := s.conn.ReadMessage()
	if err != nil {
		return "", false, err
	}
	text, final, err := parseInworldTranscript(raw)
	if err != nil {
		return "", false, fmt.Errorf("inworld stt response: %w", err)
	}
	return text, final, nil
}

func (s *InworldSTTStream) Close() {
	_ = s.conn.WriteJSON(map[string]any{"close_stream": map[string]any{}})
	_ = s.conn.Close()
}

func parseInworldTranscript(raw []byte) (string, bool, error) {
	var value map[string]any
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false, nil
	}
	if code, ok := value["code"]; ok && code != nil {
		return "", false, fmt.Errorf("%v", value["message"])
	}
	if errValue, ok := value["error"].(map[string]any); ok && errValue["code"] != nil {
		return "", false, fmt.Errorf("%v", errValue["message"])
	}
	transcription := map[string]any(nil)
	if result, ok := value["result"].(map[string]any); ok {
		if nested, ok := result["transcription"].(map[string]any); ok {
			transcription = nested
		}
	}
	if transcription == nil {
		if nested, ok := value["transcription"].(map[string]any); ok {
			transcription = nested
		}
	}
	if transcription == nil {
		return "", false, nil
	}
	text, _ := transcription["transcript"].(string)
	text = strings.Join(strings.Fields(text), " ")
	if text == "" {
		return "", false, nil
	}
	final, _ := transcription["isFinal"].(bool)
	if !final {
		final, _ = transcription["is_final"].(bool)
	}
	return text, final, nil
}

func inworldAuthorization(apiKey string) string {
	if strings.HasPrefix(strings.ToLower(apiKey), "basic ") {
		return apiKey
	}
	return "Basic " + apiKey
}

func pcmToWAV(pcm []byte) []byte {
	var out bytes.Buffer
	dataLen := uint32(len(pcm))
	byteRate := uint32(audioSampleRate * audioChannels * audioBitDepth / 8)
	blockAlign := uint16(audioChannels * audioBitDepth / 8)
	out.WriteString("RIFF")
	_ = binary.Write(&out, binary.LittleEndian, uint32(36)+dataLen)
	out.WriteString("WAVEfmt ")
	_ = binary.Write(&out, binary.LittleEndian, uint32(16))
	_ = binary.Write(&out, binary.LittleEndian, uint16(1))
	_ = binary.Write(&out, binary.LittleEndian, uint16(audioChannels))
	_ = binary.Write(&out, binary.LittleEndian, uint32(audioSampleRate))
	_ = binary.Write(&out, binary.LittleEndian, byteRate)
	_ = binary.Write(&out, binary.LittleEndian, blockAlign)
	_ = binary.Write(&out, binary.LittleEndian, uint16(audioBitDepth))
	out.WriteString("data")
	_ = binary.Write(&out, binary.LittleEndian, dataLen)
	out.Write(pcm)
	return out.Bytes()
}

func wavToPCM(wav []byte) ([]byte, error) {
	if len(wav) < 44 || !bytes.HasPrefix(wav, []byte("RIFF")) {
		return nil, errors.New("not a wav file")
	}
	pos := 12
	for pos+8 <= len(wav) {
		chunkID := string(wav[pos : pos+4])
		chunkSize := int(binary.LittleEndian.Uint32(wav[pos+4 : pos+8]))
		pos += 8
		if chunkSize < 0 || pos+chunkSize > len(wav) {
			return nil, errors.New("bad wav chunk size")
		}
		if chunkID == "data" {
			return wav[pos : pos+chunkSize], nil
		}
		pos += chunkSize
		if pos%2 == 1 {
			pos++
		}
	}
	return nil, errors.New("wav data chunk not found")
}
