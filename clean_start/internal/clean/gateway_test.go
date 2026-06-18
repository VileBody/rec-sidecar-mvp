package clean

import "testing"

func TestBrowserTranscriptRejectReason(t *testing.T) {
	tests := []struct {
		name string
		text string
		want string
	}{
		{name: "keeps russian speech", text: "Ну и почему ничего не пишут?", want: ""},
		{name: "drops single latin letter", text: "D", want: "too_short"},
		{name: "drops latin hallucination", text: "Verkosveezi.", want: "no_cyrillic"},
		{name: "drops japanese hallucination", text: "愛を射抜いたのさ。", want: "non_russian_script"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := browserTranscriptRejectReason(tt.text); got != tt.want {
				t.Fatalf("browserTranscriptRejectReason(%q) = %q, want %q", tt.text, got, tt.want)
			}
		})
	}
}

func TestSellerEchoRejectReason(t *testing.T) {
	sessionID := "sess-test"
	g := &Gateway{store: NewStore()}
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	g.store.Apply(NewEvent(sessionID, EventSellerInput, "test", TextData{
		Text: "Понимаю, давайте сначала быстро сверим, что именно сейчас не работает.",
	}))

	if got := g.sellerEchoRejectReason(sessionID, "client", "browser-system-audio", "Понимаю давайте сначала быстро сверим что именно сейчас не работает"); got != "seller_echo_message" {
		t.Fatalf("seller echo reason = %q, want seller_echo_message", got)
	}
	if got := g.sellerEchoRejectReason(sessionID, "client", "browser-system-audio", "Нет, я как клиент хочу уточнить совсем другой вопрос"); got != "" {
		t.Fatalf("ordinary client text rejected as %q", got)
	}
	if got := g.sellerEchoRejectReason(sessionID, "seller", "browser-microphone-test", "Понимаю давайте сначала быстро сверим"); got != "" {
		t.Fatalf("seller mic text rejected as %q", got)
	}
}
