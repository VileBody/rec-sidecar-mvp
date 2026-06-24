package clean

import (
	"strings"
	"time"
	"unicode"
)

const (
	echoRecentWindow     = 5 * time.Second
	echoLongWindow       = 45 * time.Second
	echoRecentSimilarity = 0.72
	echoLongSimilarity   = 0.88
)

func suppressSystemSellerSegment(enabled bool, source, role string) bool {
	return enabled && normalizeCaptureSource(source) == CaptureSourceRemoteAudio && role == "seller"
}

func (g *Gateway) crossSourceEchoRejectReason(sessionID, role, source, text string) string {
	if reason := g.sellerEchoRejectReason(sessionID, role, source, text); reason != "" {
		return reason
	}
	if reason := g.clientEchoRejectReason(sessionID, role, source, text); reason != "" {
		return reason
	}
	return ""
}

func (g *Gateway) sellerEchoRejectReason(sessionID, role, source, text string) string {
	if role != "client" || normalizeCaptureSource(source) != CaptureSourceRemoteAudio {
		return ""
	}
	probe := normalizeEchoText(text)
	if len([]rune(probe)) < 8 {
		return ""
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		return ""
	}
	if textSimilarity(probe, normalizeEchoText(state.SellerDraft)) >= 0.82 {
		return "seller_echo_into_remote_draft"
	}
	if textSimilarity(probe, normalizeEchoText(state.SellerDraftImmediate)) >= 0.82 {
		return "seller_echo_into_remote_immediate_draft"
	}
	now := time.Now()
	for i := len(state.Messages) - 1; i >= 0; i-- {
		msg := state.Messages[i]
		if msg.Role != "seller" {
			continue
		}
		age := now.Sub(msg.CreatedAt)
		if age > echoLongWindow {
			break
		}
		threshold := echoLongSimilarity
		reason := "seller_echo_into_remote_long_message"
		if age <= echoRecentWindow {
			threshold = echoRecentSimilarity
			reason = "seller_echo_into_remote_recent_message"
		}
		if textSimilarity(probe, normalizeEchoText(msg.Text)) >= threshold {
			return reason
		}
	}
	for i := len(state.Transcript) - 1; i >= 0; i-- {
		item := state.Transcript[i]
		if item.Role != "seller" {
			continue
		}
		age := now.Sub(item.CreatedAt)
		if age > echoLongWindow {
			break
		}
		threshold := echoLongSimilarity
		reason := "seller_echo_into_remote_long_transcript"
		if age <= echoRecentWindow {
			threshold = echoRecentSimilarity
			reason = "seller_echo_into_remote_recent_transcript"
		}
		if textSimilarity(probe, normalizeEchoText(item.Text)) >= threshold {
			return reason
		}
	}
	return ""
}

func (g *Gateway) clientEchoRejectReason(sessionID, role, source, text string) string {
	if role != "seller" || normalizeCaptureSource(source) != CaptureSourceSellerMic {
		return ""
	}
	probe := normalizeEchoText(text)
	if len([]rune(probe)) < 8 {
		return ""
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		return ""
	}
	now := time.Now()
	for i := len(state.Messages) - 1; i >= 0; i-- {
		msg := state.Messages[i]
		if msg.Role != "client" {
			continue
		}
		age := now.Sub(msg.CreatedAt)
		if age > echoLongWindow {
			break
		}
		threshold := echoLongSimilarity
		reason := "client_echo_into_mic_long_message"
		if age <= echoRecentWindow {
			threshold = echoRecentSimilarity
			reason = "client_echo_into_mic_recent_message"
		}
		if textSimilarity(probe, normalizeEchoText(msg.Text)) >= threshold {
			return reason
		}
	}
	for i := len(state.Transcript) - 1; i >= 0; i-- {
		item := state.Transcript[i]
		if item.Role != "client" {
			continue
		}
		age := now.Sub(item.CreatedAt)
		if age > echoLongWindow {
			break
		}
		threshold := echoLongSimilarity
		reason := "client_echo_into_mic_long_transcript"
		if age <= echoRecentWindow {
			threshold = echoRecentSimilarity
			reason = "client_echo_into_mic_recent_transcript"
		}
		if textSimilarity(probe, normalizeEchoText(item.Text)) >= threshold {
			return reason
		}
	}
	return ""
}

func normalizeEchoText(text string) string {
	var b strings.Builder
	lastSpace := true
	for _, r := range strings.ToLower(text) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
			lastSpace = false
			continue
		}
		if !lastSpace {
			b.WriteRune(' ')
			lastSpace = true
		}
	}
	return strings.TrimSpace(b.String())
}

func textSimilarity(a, b string) float64 {
	if a == "" || b == "" {
		return 0
	}
	if strings.Contains(a, b) || strings.Contains(b, a) {
		shorter := len([]rune(a))
		longer := len([]rune(b))
		if shorter > longer {
			shorter, longer = longer, shorter
		}
		if longer == 0 {
			return 0
		}
		return float64(shorter) / float64(longer)
	}
	aTokens := tokenSet(a)
	bTokens := tokenSet(b)
	if len(aTokens) == 0 || len(bTokens) == 0 {
		return 0
	}
	intersections := 0
	for token := range aTokens {
		if _, ok := bTokens[token]; ok {
			intersections++
		}
	}
	return float64(2*intersections) / float64(len(aTokens)+len(bTokens))
}

func tokenSet(text string) map[string]struct{} {
	out := make(map[string]struct{})
	for _, token := range strings.Fields(text) {
		if len([]rune(token)) < 2 {
			continue
		}
		out[token] = struct{}{}
	}
	return out
}
