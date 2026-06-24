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

type echoRejectMatch struct {
	Reason string
	Score  float64
}

func (m echoRejectMatch) Found() bool {
	return m.Reason != ""
}

func suppressSystemSellerSegment(enabled bool, source, role string) bool {
	return enabled && normalizeCaptureSource(source) == CaptureSourceRemoteAudio && role == "seller"
}

func (g *Gateway) crossSourceEchoRejectReason(sessionID, role, source, text string) string {
	return g.crossSourceEchoRejectMatch(sessionID, role, source, text).Reason
}

func (g *Gateway) crossSourceEchoRejectMatch(sessionID, role, source, text string) echoRejectMatch {
	if match := g.sellerEchoRejectMatch(sessionID, role, source, text); match.Found() {
		return match
	}
	if match := g.clientEchoRejectMatch(sessionID, role, source, text); match.Found() {
		return match
	}
	return echoRejectMatch{}
}

func (g *Gateway) sellerEchoRejectReason(sessionID, role, source, text string) string {
	return g.sellerEchoRejectMatch(sessionID, role, source, text).Reason
}

func (g *Gateway) sellerEchoRejectMatch(sessionID, role, source, text string) echoRejectMatch {
	if role != "client" || normalizeCaptureSource(source) != CaptureSourceRemoteAudio {
		return echoRejectMatch{}
	}
	probe := normalizeEchoText(text)
	if len([]rune(probe)) < 8 {
		return echoRejectMatch{}
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		return echoRejectMatch{}
	}
	if score := textSimilarity(probe, normalizeEchoText(state.SellerDraft)); score >= 0.82 {
		return echoRejectMatch{Reason: "seller_echo_into_remote_draft", Score: score}
	}
	if score := textSimilarity(probe, normalizeEchoText(state.SellerDraftImmediate)); score >= 0.82 {
		return echoRejectMatch{Reason: "seller_echo_into_remote_immediate_draft", Score: score}
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
		if score := textSimilarity(probe, normalizeEchoText(msg.Text)); score >= threshold {
			return echoRejectMatch{Reason: reason, Score: score}
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
		if score := textSimilarity(probe, normalizeEchoText(item.Text)); score >= threshold {
			return echoRejectMatch{Reason: reason, Score: score}
		}
	}
	return echoRejectMatch{}
}

func (g *Gateway) clientEchoRejectReason(sessionID, role, source, text string) string {
	return g.clientEchoRejectMatch(sessionID, role, source, text).Reason
}

func (g *Gateway) clientEchoRejectMatch(sessionID, role, source, text string) echoRejectMatch {
	if role != "seller" || normalizeCaptureSource(source) != CaptureSourceSellerMic {
		return echoRejectMatch{}
	}
	probe := normalizeEchoText(text)
	if len([]rune(probe)) < 8 {
		return echoRejectMatch{}
	}
	state, ok := g.store.Get(sessionID)
	if !ok {
		return echoRejectMatch{}
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
		if score := textSimilarity(probe, normalizeEchoText(msg.Text)); score >= threshold {
			return echoRejectMatch{Reason: reason, Score: score}
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
		if score := textSimilarity(probe, normalizeEchoText(item.Text)); score >= threshold {
			return echoRejectMatch{Reason: reason, Score: score}
		}
	}
	return echoRejectMatch{}
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
