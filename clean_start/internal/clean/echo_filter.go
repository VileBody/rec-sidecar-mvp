package clean

import (
	"strings"
	"time"
	"unicode"
)

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
	if role != "client" || source != "browser-system-audio" {
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
		if msg.Role != "seller" {
			continue
		}
		if now.Sub(msg.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(msg.Text)) >= 0.82 {
			return "seller_echo_message"
		}
	}
	for i := len(state.Transcript) - 1; i >= 0; i-- {
		item := state.Transcript[i]
		if item.Role != "seller" {
			continue
		}
		if now.Sub(item.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(item.Text)) >= 0.82 {
			return "seller_echo_transcript"
		}
	}
	return ""
}

func (g *Gateway) clientEchoRejectReason(sessionID, role, source, text string) string {
	if role != "seller" || source != "browser-microphone-test" {
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
		if now.Sub(msg.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(msg.Text)) >= 0.82 {
			return "client_echo_message"
		}
	}
	for i := len(state.Transcript) - 1; i >= 0; i-- {
		item := state.Transcript[i]
		if item.Role != "client" {
			continue
		}
		if now.Sub(item.CreatedAt) > 45*time.Second {
			break
		}
		if textSimilarity(probe, normalizeEchoText(item.Text)) >= 0.82 {
			return "client_echo_transcript"
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
