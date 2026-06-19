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

func TestRoleForSTTSpeakerMixed(t *testing.T) {
	roles := map[string]string{}
	if got := roleForSTTSpeaker("mixed", "1", roles); got != "seller" {
		t.Fatalf("first mixed speaker role = %q, want seller", got)
	}
	if got := roleForSTTSpeaker("mixed", "2", roles); got != "client" {
		t.Fatalf("second mixed speaker role = %q, want client", got)
	}
	if got := roleForSTTSpeaker("mixed", "1", roles); got != "seller" {
		t.Fatalf("known first mixed speaker role = %q, want seller", got)
	}
	if got := roleForSTTSpeaker("client", "1", roles); got != "client" {
		t.Fatalf("explicit client role = %q, want client", got)
	}
}

func TestParseInworldTranscriptSpeakerSegments(t *testing.T) {
	raw := []byte(`{
		"result": {
			"transcription": {
				"transcript": "Привет Да, слушаю",
				"isFinal": true,
				"wordTimestamps": [
					{"word":"При","speaker":1},
					{"word":"вет","speaker":1},
					{"word":" Да","speaker":2},
					{"word":",","speaker":2},
					{"word":" слушаю","speaker":2}
				]
			}
		}
	}`)
	transcript, err := parseInworldTranscript(raw)
	if err != nil {
		t.Fatal(err)
	}
	if transcript.Text != "Привет Да, слушаю" || !transcript.Final {
		t.Fatalf("unexpected transcript: %#v", transcript)
	}
	if len(transcript.Segments) != 2 {
		t.Fatalf("segments len = %d, want 2: %#v", len(transcript.Segments), transcript.Segments)
	}
	if transcript.Segments[0].Speaker != "1" || transcript.Segments[0].Text != "Привет" {
		t.Fatalf("segment 0 = %#v", transcript.Segments[0])
	}
	if transcript.Segments[1].Speaker != "2" || transcript.Segments[1].Text != "Да, слушаю" {
		t.Fatalf("segment 1 = %#v", transcript.Segments[1])
	}
}
