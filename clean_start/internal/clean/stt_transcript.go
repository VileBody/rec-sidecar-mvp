package clean

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"
	"unicode"

	"github.com/gorilla/websocket"
)

func transcribePCMWithStream(stream STTStream, provider string, pcm []byte) (string, error) {
	if len(pcm) == 0 {
		return "", errors.New("empty pcm")
	}
	if err := stream.SendAudio(pcm); err != nil {
		return "", err
	}
	if err := stream.SendEndTurn(); err != nil {
		return "", fmt.Errorf("%s stt end_turn: %w", provider, err)
	}

	_ = stream.SetReadDeadline(time.Now().Add(8 * time.Second))
	var lastPartial string
	for {
		transcript, err := stream.ReadTranscript()
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
			return "", fmt.Errorf("%s stt read: %w", provider, err)
		}
		text := transcript.Text
		if text == "" {
			continue
		}
		if transcript.Final {
			return text, nil
		}
		lastPartial = text
	}
}

func browserTranscriptRejectReason(text string) string {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return "empty"
	}

	letters := 0
	cyrillic := 0
	nonRussianScript := 0
	for _, r := range trimmed {
		if !unicode.IsLetter(r) {
			continue
		}
		letters++
		switch {
		case unicode.In(r, unicode.Cyrillic):
			cyrillic++
		case isCJKLike(r):
			nonRussianScript++
		}
	}

	if letters <= 1 {
		return "too_short"
	}
	if cyrillic == 0 {
		if nonRussianScript > 0 {
			return "non_russian_script"
		}
		if latinPhraseLooksIntentional(trimmed) {
			return ""
		}
		return "no_cyrillic"
	}
	if nonRussianScript > cyrillic {
		return "mostly_non_russian_script"
	}
	return ""
}

func latinPhraseLooksIntentional(text string) bool {
	words := 0
	for _, field := range strings.Fields(text) {
		letters := 0
		for _, r := range field {
			if unicode.IsLetter(r) {
				letters++
			}
		}
		if letters > 0 {
			words++
		}
	}
	return words >= 3
}

func diarizedTranscriptSegments(transcript STTTranscript) []STTSegment {
	if len(transcript.Segments) == 0 {
		return []STTSegment{{Text: transcript.Text}}
	}
	return transcript.Segments
}

type sttSegmentTracker struct {
	turn int
}

func newSTTSegmentTracker() *sttSegmentTracker {
	return &sttSegmentTracker{}
}

func (t *sttSegmentTracker) ID(segment STTSegment, index int) string {
	return transcriptSegmentID(t.turn, segment, index)
}

func (t *sttSegmentTracker) NextTurn() {
	t.turn++
}

func transcriptSegmentID(turn int, segment STTSegment, index int) string {
	speaker := sanitizeSpeakerID(segment.Speaker)
	if speaker == "" {
		speaker = "unknown"
	}
	return fmt.Sprintf("%04d-%03d-%s", turn, index, speaker)
}

type sttStreamStabilizer struct {
	segments map[string]sttSegmentState
}

type sttSegmentState struct {
	text  string
	final bool
}

func newSTTStreamStabilizer() *sttStreamStabilizer {
	return &sttStreamStabilizer{segments: make(map[string]sttSegmentState)}
}

func (s *sttStreamStabilizer) ShouldEmit(segmentID, text string, final bool) bool {
	normalized := strings.Join(strings.Fields(text), " ")
	if normalized == "" {
		return false
	}
	previous, ok := s.segments[segmentID]
	if ok {
		if previous.text == normalized {
			if final && !previous.final {
				s.segments[segmentID] = sttSegmentState{text: normalized, final: true}
				return true
			}
			return false
		}
		if !final && !previous.final && len([]rune(normalized)) < len([]rune(previous.text)) && strings.HasPrefix(previous.text, normalized) {
			return false
		}
	}
	s.segments[segmentID] = sttSegmentState{text: normalized, final: final}
	return true
}

func roleForCaptureSource(source string) (string, bool) {
	switch strings.TrimSpace(source) {
	case "browser-microphone-test":
		return "seller", true
	case "browser-system-audio":
		return "client", true
	default:
		return "", false
	}
}

func roleForSTTSource(defaultRole, source, speaker string, speakerRoles map[string]string) string {
	if role, ok := roleForCaptureSource(source); ok {
		return role
	}
	return roleForSTTSpeaker(defaultRole, speaker, speakerRoles)
}

func roleForSTTSpeaker(defaultRole, speaker string, speakerRoles map[string]string) string {
	defaultRole = strings.TrimSpace(defaultRole)
	if defaultRole != "mixed" {
		return defaultRole
	}
	speaker = normalizeQuerySpeaker(speaker)
	if speaker == "" {
		return "speaker"
	}
	if role, ok := speakerRoles[speaker]; ok {
		return role
	}
	role := "speaker_" + sanitizeSpeakerID(speaker)
	speakerRoles[speaker] = role
	return role
}

func speakerRolesFromQuery(values url.Values) map[string]string {
	roles := map[string]string{}
	sellerSpeaker := normalizeQuerySpeaker(values.Get("seller_speaker"))
	clientSpeaker := normalizeQuerySpeaker(values.Get("client_speaker"))
	if sellerSpeaker != "" {
		roles[sellerSpeaker] = "seller"
	}
	if clientSpeaker != "" && clientSpeaker != sellerSpeaker {
		roles[clientSpeaker] = "client"
	}
	if clientSpeaker == "" {
		switch sellerSpeaker {
		case "1":
			roles["2"] = "client"
		case "2":
			roles["1"] = "client"
		}
	}
	return roles
}

func normalizeQuerySpeaker(raw string) string {
	value := strings.ToLower(strings.TrimSpace(raw))
	if value == "" || value == "auto" || value == "unknown" {
		return ""
	}
	for _, prefix := range []string{"speaker_", "speaker-", "speaker", "spk_", "spk-", "spk"} {
		value = strings.TrimPrefix(value, prefix)
	}
	return sanitizeSpeakerID(value)
}

func sanitizeSpeakerID(speaker string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(speaker) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

func isCJKLike(r rune) bool {
	return (r >= 0x3040 && r <= 0x30ff) || // Hiragana/Katakana
		(r >= 0x3400 && r <= 0x9fff) || // CJK ideographs
		(r >= 0xac00 && r <= 0xd7af) // Hangul
}
