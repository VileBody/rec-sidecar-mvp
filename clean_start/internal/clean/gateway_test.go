package clean

import (
	"io"
	"log/slog"
	"net/url"
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

func TestSpeakerRolesFromQueryMapsSellerAndClient(t *testing.T) {
	roles := speakerRolesFromQuery(url.Values{"seller_speaker": []string{"speaker_2"}})
	if got := roleForSTTSpeaker("mixed", "2", roles); got != "seller" {
		t.Fatalf("speaker 2 role = %q, want seller", got)
	}
	if got := roleForSTTSpeaker("mixed", "speaker_2", roles); got != "seller" {
		t.Fatalf("normalized speaker_2 role = %q, want seller", got)
	}
	if got := roleForSTTSpeaker("mixed", "1", roles); got != "client" {
		t.Fatalf("speaker 1 role = %q, want client", got)
	}
	if got := roleForSTTSpeaker("mixed", "3", roles); got != "speaker_3" {
		t.Fatalf("speaker 3 role = %q, want speaker_3", got)
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

func TestAppendTranscriptCoalescesInterleavedDiarizedPartials(t *testing.T) {
	now := time.Now().UTC()
	spk2 := TranscriptItem{ID: "partial-1", Role: "speaker_2", Speaker: "2", Source: "browser-system-audio", SegmentID: "000-2", Text: "Хм.", CreatedAt: now}
	spk1 := TranscriptItem{ID: "partial-2", Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "001-1", Text: "Согласен. Ты всё равно ниндзя.", CreatedAt: now}
	items := []TranscriptItem{}

	items = appendTranscript(items, spk2)
	items = appendTranscript(items, spk1)
	items = appendTranscript(items, TranscriptItem{ID: "partial-3", Role: spk2.Role, Speaker: spk2.Speaker, Source: spk2.Source, SegmentID: spk2.SegmentID, Text: spk2.Text, CreatedAt: now.Add(time.Second)})
	items = appendTranscript(items, TranscriptItem{ID: "partial-4", Role: spk1.Role, Speaker: spk1.Speaker, Source: spk1.Source, SegmentID: spk1.SegmentID, Text: spk1.Text, CreatedAt: now.Add(time.Second)})

	if len(items) != 2 {
		t.Fatalf("repeated interleaved partials should stay two bubbles, got %#v", items)
	}
	if items[0].ID != spk2.ID || !items[0].CreatedAt.Equal(now) {
		t.Fatalf("first bubble identity should stay stable: %#v", items[0])
	}
	if items[0].Text != spk2.Text || items[1].Text != spk1.Text {
		t.Fatalf("unexpected partial ordering/content: %#v", items)
	}

	items = appendTranscript(items, TranscriptItem{ID: "final-1", Role: spk2.Role, Speaker: spk2.Speaker, Source: spk2.Source, SegmentID: spk2.SegmentID, Text: spk2.Text, Final: true, CreatedAt: now.Add(2 * time.Second)})
	items = appendTranscript(items, TranscriptItem{ID: "final-2", Role: spk1.Role, Speaker: spk1.Speaker, Source: spk1.Source, SegmentID: spk1.SegmentID, Text: spk1.Text, Final: true, CreatedAt: now.Add(2 * time.Second)})
	if len(items) != 2 || !items[0].Final || !items[1].Final {
		t.Fatalf("finals should replace matching partials without duplicates: %#v", items)
	}
	if items[0].ID != spk2.ID || !items[0].CreatedAt.Equal(now) {
		t.Fatalf("final should preserve partial identity/time: %#v", items[0])
	}

	items = appendTranscript(items, TranscriptItem{Role: spk1.Role, Speaker: spk1.Speaker, Source: spk1.Source, SegmentID: spk1.SegmentID, Text: "Тогда разговор окончен.", CreatedAt: now.Add(7 * time.Second)})
	if len(items) != 3 || items[2].Final {
		t.Fatalf("new partial after final should start a new bubble: %#v", items)
	}
}

func TestAppendTranscriptAllowsRetroactiveFinalCorrection(t *testing.T) {
	now := time.Now().UTC()
	items := []TranscriptItem{}
	items = appendTranscript(items, TranscriptItem{ID: "partial-1", Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0000-000-1", Text: "Давай дзюцу при", CreatedAt: now})
	items = appendTranscript(items, TranscriptItem{ID: "final-1", Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0000-000-1", Text: "Давай дзюцу призыва.", Final: true, CreatedAt: now.Add(7 * time.Second)})
	items = appendTranscript(items, TranscriptItem{ID: "final-correction", Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0000-000-1", Text: "Давай дзюцу призыва!", Final: true, CreatedAt: now.Add(8 * time.Second)})

	if len(items) != 1 {
		t.Fatalf("correction should update existing bubble, got %#v", items)
	}
	if items[0].Text != "Давай дзюцу призыва!" || !items[0].Final {
		t.Fatalf("unexpected corrected item: %#v", items[0])
	}
	if items[0].ID != "partial-1" || !items[0].CreatedAt.Equal(now) {
		t.Fatalf("correction should preserve bubble identity/time: %#v", items[0])
	}

	items = appendTranscript(items, TranscriptItem{Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0001-000-1", Text: "Новая реплика.", CreatedAt: now.Add(10 * time.Second)})
	if len(items) != 2 || items[1].Text != "Новая реплика." {
		t.Fatalf("new turn should append, not overwrite old bubble: %#v", items)
	}
}

func TestAppendTranscriptReplacesSameTextFinalWithChangedSegmentID(t *testing.T) {
	now := time.Now().UTC()
	items := []TranscriptItem{}
	items = appendTranscript(items, TranscriptItem{
		ID:        "partial-1",
		Role:      "student_original",
		Speaker:   "unknown",
		Source:    "student-system-audio",
		SegmentID: "0000-000-unknown",
		Text:      "А где твои вещи, Моко?",
		CreatedAt: now,
	})
	items = appendTranscript(items, TranscriptItem{
		ID:        "final-1",
		Role:      "student_original",
		Speaker:   "unknown",
		Source:    "student-system-audio",
		SegmentID: "0001-000-unknown",
		Text:      "А где твои вещи, Моко?",
		Final:     true,
		CreatedAt: now.Add(2 * time.Second),
	})

	if len(items) != 1 {
		t.Fatalf("same text final should replace partial, got %#v", items)
	}
	if items[0].ID != "partial-1" || !items[0].Final || items[0].SegmentID != "0001-000-unknown" {
		t.Fatalf("final should preserve bubble identity and update content: %#v", items[0])
	}
	if !items[0].CreatedAt.Equal(now) {
		t.Fatalf("created_at should stay stable: %#v", items[0])
	}
}

func TestAppendTranscriptKeepsDiarizedSegmentsFromSameSpeakerSeparate(t *testing.T) {
	now := time.Now().UTC()
	items := []TranscriptItem{}
	items = appendTranscript(items, TranscriptItem{Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0000-000-1", Text: "Первая реплика.", CreatedAt: now})
	items = appendTranscript(items, TranscriptItem{Role: "speaker_2", Speaker: "2", Source: "browser-system-audio", SegmentID: "0000-001-2", Text: "Ответ.", CreatedAt: now})
	items = appendTranscript(items, TranscriptItem{Role: "speaker_1", Speaker: "1", Source: "browser-system-audio", SegmentID: "0000-002-1", Text: "Вторая реплика того же спикера.", CreatedAt: now})

	if len(items) != 3 {
		t.Fatalf("same speaker segments should remain separate by segment_id: %#v", items)
	}
	if items[0].Text != "Первая реплика." || items[2].Text != "Вторая реплика того же спикера." {
		t.Fatalf("same speaker segments were collapsed: %#v", items)
	}
}

func TestSTTSegmentTrackerKeepsSegmentIDsUniqueAcrossTurns(t *testing.T) {
	tracker := newSTTSegmentTracker()
	first := tracker.ID(STTSegment{Speaker: "1", Text: "Первая"}, 0)
	again := tracker.ID(STTSegment{Speaker: "1", Text: "Первая поправка"}, 0)
	tracker.NextTurn()
	nextTurn := tracker.ID(STTSegment{Speaker: "1", Text: "Новая"}, 0)

	if first != "0000-000-1" {
		t.Fatalf("first id = %q, want 0000-000-1", first)
	}
	if again != first {
		t.Fatalf("same turn segment id should be stable: %q vs %q", again, first)
	}
	if nextTurn != "0001-000-1" {
		t.Fatalf("next turn id = %q, want 0001-000-1", nextTurn)
	}
}

func TestSTTStreamStabilizerSuppressesRepeatedFullHypotheses(t *testing.T) {
	stabilizer := newSTTStreamStabilizer()

	if !stabilizer.ShouldEmit("000-1", "Один из первых.", false) {
		t.Fatal("first partial should emit")
	}
	if stabilizer.ShouldEmit("000-1", "Один из первых.", false) {
		t.Fatal("same partial should be suppressed")
	}
	if !stabilizer.ShouldEmit("000-1", "Один из первых.", true) {
		t.Fatal("matching final should still emit")
	}
	if stabilizer.ShouldEmit("000-1", "Один из первых.", true) {
		t.Fatal("duplicate final should be suppressed")
	}
	if !stabilizer.ShouldEmit("001-2", "Новая реплика.", false) {
		t.Fatal("different segment should emit")
	}
}

func TestSTTAudioJitterBufferCoalescesBrowserChunks(t *testing.T) {
	stream := &fakeSTTStream{}
	stats := &sttAudioStats{}
	commands := make(chan sttAudioCommand, 3)
	commands <- sttAudioCommand{audio: make([]byte, durationToPCMBytes(50*time.Millisecond))}
	commands <- sttAudioCommand{audio: make([]byte, durationToPCMBytes(50*time.Millisecond))}
	commands <- sttAudioCommand{close: true}
	close(commands)

	if err := runSTTAudioJitterBuffer(stream, commands, stats); err != nil {
		t.Fatal(err)
	}
	if len(stream.audio) != 1 {
		t.Fatalf("expected one coalesced audio flush, got %d", len(stream.audio))
	}
	if got, want := len(stream.audio[0]), durationToPCMBytes(100*time.Millisecond); got != want {
		t.Fatalf("audio flush bytes = %d, want %d", got, want)
	}
	if stats.audioFlushes.Load() != 1 {
		t.Fatalf("audio flushes = %d, want 1", stats.audioFlushes.Load())
	}
}

func TestSTTAudioJitterBufferFlushesBeforeEndTurn(t *testing.T) {
	stream := &fakeSTTStream{}
	stats := &sttAudioStats{}
	commands := make(chan sttAudioCommand, 4)
	commands <- sttAudioCommand{audio: make([]byte, durationToPCMBytes(40*time.Millisecond))}
	commands <- sttAudioCommand{endTurn: true}
	commands <- sttAudioCommand{endTurn: true}
	commands <- sttAudioCommand{close: true}
	close(commands)

	if err := runSTTAudioJitterBuffer(stream, commands, stats); err != nil {
		t.Fatal(err)
	}
	if len(stream.audio) != 1 {
		t.Fatalf("expected pending short audio to flush before end_turn, got %d flushes", len(stream.audio))
	}
	if got, want := len(stream.audio[0]), durationToPCMBytes(40*time.Millisecond); got != want {
		t.Fatalf("short audio flush bytes = %d, want %d", got, want)
	}
	if stream.endTurns != 1 {
		t.Fatalf("end turns = %d, want 1", stream.endTurns)
	}
	if stats.coalescedEndTurns.Load() != 1 {
		t.Fatalf("coalesced end turns = %d, want 1", stats.coalescedEndTurns.Load())
	}
}

func TestSTTAudioJitterBufferCapsFlushSize(t *testing.T) {
	stream := &fakeSTTStream{}
	stats := &sttAudioStats{}
	commands := make(chan sttAudioCommand, 2)
	commands <- sttAudioCommand{audio: make([]byte, durationToPCMBytes(500*time.Millisecond))}
	commands <- sttAudioCommand{close: true}
	close(commands)

	if err := runSTTAudioJitterBuffer(stream, commands, stats); err != nil {
		t.Fatal(err)
	}
	if len(stream.audio) < 2 {
		t.Fatalf("expected large audio to split into multiple flushes, got %d", len(stream.audio))
	}
	for i, chunk := range stream.audio {
		if len(chunk) > durationToPCMBytes(sttAudioMaxFlush) {
			t.Fatalf("flush %d was %d bytes, over max %d", i, len(chunk), durationToPCMBytes(sttAudioMaxFlush))
		}
	}
}

func TestEnqueueSTTAudioCommandHalfFlushesQueuedAudio(t *testing.T) {
	stats := &sttAudioStats{}
	commands := make(chan sttAudioCommand, 64)
	chunk := make([]byte, durationToPCMBytes(50*time.Millisecond))
	for i := 0; i < sttAudioQueueCriticalCommands; i++ {
		commands <- sttAudioCommand{audio: chunk}
	}
	commands <- sttAudioCommand{endTurn: true}

	enqueueSTTAudioCommand(commands, sttAudioCommand{audio: chunk}, stats)

	if stats.audioQueueFlushes.Load() != 1 {
		t.Fatalf("queue half flushes = %d, want 1", stats.audioQueueFlushes.Load())
	}
	if stats.audioDroppedQueueCommands.Load() == 0 {
		t.Fatal("expected old queued audio commands to be dropped")
	}
	if stats.audioDroppedQueueBytes.Load() == 0 {
		t.Fatal("expected old queued audio bytes to be dropped")
	}
	if got, max := len(commands), sttAudioQueueTargetCommands+2; got > max {
		t.Fatalf("queued commands after half flush = %d, want <= %d", got, max)
	}

	foundEndTurn := false
	for len(commands) > 0 {
		if (<-commands).endTurn {
			foundEndTurn = true
			break
		}
	}
	if !foundEndTurn {
		t.Fatal("expected queued end_turn control command to be preserved")
	}
}

type fakeSTTStream struct {
	audio    [][]byte
	endTurns int
}

func (s *fakeSTTStream) SendAudio(pcm []byte) error {
	copied := make([]byte, len(pcm))
	copy(copied, pcm)
	s.audio = append(s.audio, copied)
	return nil
}

func (s *fakeSTTStream) SendEndTurn() error {
	s.endTurns++
	return nil
}

func (s *fakeSTTStream) ReadTranscript() (STTTranscript, error) {
	return STTTranscript{}, nil
}

func (s *fakeSTTStream) SetReadDeadline(time.Time) error {
	return nil
}

func (s *fakeSTTStream) Close() {}

func TestAppendTranscriptDropsEmptyText(t *testing.T) {
	items := appendTranscript(nil, TranscriptItem{Role: "speaker_1", Speaker: "1", Source: "browser-system-audio"})
	if len(items) != 0 {
		t.Fatalf("empty transcript item should be ignored: %#v", items)
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
