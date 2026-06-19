package clean

import (
	"io"
	"log/slog"
	"testing"
	"time"
)

func noopLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

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

func TestClientEchoRejectReason(t *testing.T) {
	sessionID := "sess-client-echo-test"
	g := &Gateway{store: NewStore()}
	g.store.Apply(NewEvent(sessionID, EventSessionCreated, "test", map[string]any{}))
	g.store.Apply(NewEvent(sessionID, EventSTTFinal, "test", SpeechData{
		Role:   "client",
		Source: "browser-system-audio",
		Text:   "Сомневаюсь, что план будет рабочим, до внедрения я обычно не дохожу.",
	}))

	if got := g.crossSourceEchoRejectReason(sessionID, "seller", "browser-microphone-test", "Сомневаюсь что план будет рабочим до внедрения я обычно не дохожу"); got != "client_echo_message" {
		t.Fatalf("client echo reason = %q, want client_echo_message", got)
	}
	if got := g.crossSourceEchoRejectReason(sessionID, "seller", "browser-microphone-test", "Да, понимаю, поэтому давайте разберем, где именно вы обычно застреваете."); got != "" {
		t.Fatalf("ordinary seller text rejected as %q", got)
	}
	if got := g.crossSourceEchoRejectReason(sessionID, "client", "browser-system-audio", "Сомневаюсь что план будет рабочим"); got != "" {
		t.Fatalf("client system text rejected as %q", got)
	}
}

func TestRoleForSTTSpeakerMixed(t *testing.T) {
	roles := map[string]string{}
	if got := roleForSTTSpeaker("mixed", "1", roles); got != "speaker_1" {
		t.Fatalf("first mixed speaker role = %q, want speaker_1", got)
	}
	if got := roleForSTTSpeaker("mixed", "2", roles); got != "speaker_2" {
		t.Fatalf("second mixed speaker role = %q, want speaker_2", got)
	}
	if got := roleForSTTSpeaker("mixed", "1", roles); got != "speaker_1" {
		t.Fatalf("known first mixed speaker role = %q, want speaker_1", got)
	}
	if got := roleForSTTSpeaker("client", "1", roles); got != "client" {
		t.Fatalf("explicit client role = %q, want client", got)
	}
}

func TestAppendTranscriptKeepsDifferentSpeakersSeparate(t *testing.T) {
	now := time.Now().UTC()
	items := appendTranscript(nil, TranscriptItem{
		Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", Text: "первая", CreatedAt: now,
	})
	items = appendTranscript(items, TranscriptItem{
		Role: "speaker_2", Speaker: "2", Source: "browser-system-audio", Text: "вторая", CreatedAt: now.Add(time.Second),
	})
	if len(items) != 2 {
		t.Fatalf("different speakers were collapsed: %#v", items)
	}
	items = appendTranscript(items, TranscriptItem{
		Role: "speaker_2", Speaker: "2", Source: "browser-system-audio", Text: "вторая правка", CreatedAt: now.Add(2 * time.Second),
	})
	if len(items) != 2 || items[1].Text != "вторая правка" {
		t.Fatalf("same speaker partial should replace last item: %#v", items)
	}
}

func TestSelectedSTTProvider(t *testing.T) {
	t.Run("auto prefers soniox", func(t *testing.T) {
		g := NewGateway(Config{STTProvider: "auto", SonioxAPIKey: "soniox", InworldAPIKey: "inworld"}, nil, NewInworldClient(Config{InworldAPIKey: "inworld"}), noopLogger())
		provider, configured := g.selectedSTTProvider()
		if provider != "soniox" || !configured {
			t.Fatalf("provider=%q configured=%v, want soniox true", provider, configured)
		}
	})
	t.Run("auto falls back to inworld", func(t *testing.T) {
		cfg := Config{STTProvider: "auto", InworldAPIKey: "inworld"}
		g := NewGateway(cfg, nil, NewInworldClient(cfg), noopLogger())
		provider, configured := g.selectedSTTProvider()
		if provider != "inworld" || !configured {
			t.Fatalf("provider=%q configured=%v, want inworld true", provider, configured)
		}
	})
	t.Run("forced soniox requires key", func(t *testing.T) {
		cfg := Config{STTProvider: "soniox", InworldAPIKey: "inworld"}
		g := NewGateway(cfg, nil, NewInworldClient(cfg), noopLogger())
		provider, configured := g.selectedSTTProvider()
		if provider != "soniox" || configured {
			t.Fatalf("provider=%q configured=%v, want soniox false", provider, configured)
		}
	})
}

func TestParseSonioxTranscriptDedupesFinalTokens(t *testing.T) {
	stream := &SonioxSTTStream{seenFinal: map[string]struct{}{}}
	raw := []byte(`{
		"tokens": [
			{"text":"При","start_ms":10,"end_ms":100,"is_final":true,"speaker":"1"},
			{"text":"вет","start_ms":100,"end_ms":200,"is_final":true,"speaker":"1"},
			{"text":" Да","start_ms":300,"end_ms":400,"is_final":true,"speaker":"2"}
		]
	}`)
	transcript, err := stream.parseTranscript(raw)
	if err != nil {
		t.Fatal(err)
	}
	if transcript.Final || transcript.Text != "Привет Да" || len(transcript.Segments) != 2 {
		t.Fatalf("unexpected partial transcript: %#v", transcript)
	}
	transcript, err = stream.parseTranscript(raw)
	if err != nil {
		t.Fatal(err)
	}
	if transcript.Text != "" {
		t.Fatalf("duplicate final tokens were emitted: %#v", transcript)
	}

	transcript, err = stream.parseTranscript([]byte(`{
		"tokens": [
			{"text":"<fin>","start_ms":400,"end_ms":400,"is_final":true,"speaker":"2"}
		]
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if !transcript.Final || transcript.Text != "Привет Да" || len(transcript.Segments) != 2 {
		t.Fatalf("unexpected transcript: %#v", transcript)
	}
	transcript, err = stream.parseTranscript([]byte(`{
		"tokens": [
			{"text":"Еще","start_ms":500,"end_ms":600,"is_final":true,"speaker":"1"},
			{"text":" раз","start_ms":600,"end_ms":700,"is_final":true,"speaker":"1"}
		]
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if transcript.Final || transcript.Text != "Еще раз" {
		t.Fatalf("next utterance should start accumulating again: %#v", transcript)
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

func TestStagePartialGate(t *testing.T) {
	w := &StageWorker{cfg: Config{MinStageChars: 5, MinStageGrowth: 5}}
	state := &stageSessionState{}

	if w.shouldUsePartialLocked(state, "abcd") {
		t.Fatal("short partial should not trigger stage detection")
	}
	if !w.shouldUsePartialLocked(state, "abcdef") {
		t.Fatal("first long partial should trigger stage detection")
	}
	if w.shouldUsePartialLocked(state, "abcdefgh") {
		t.Fatal("tiny prefix growth should not trigger stage detection")
	}
	if !w.shouldUsePartialLocked(state, "abcdefghi?") {
		t.Fatal("sentence-ending partial should trigger stage detection")
	}
	if !w.shouldUsePartialLocked(state, "значимо исправленный текст") {
		t.Fatal("non-prefix STT correction should trigger stage detection")
	}
}
