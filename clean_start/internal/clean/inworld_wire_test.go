package clean

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestInworldWireMessagesUseDocumentedCamelCase(t *testing.T) {
	cfg := Config{InworldSTTModel: "soniox/stt-rt-v4", InworldSTTLanguage: "ru"}
	tests := []struct {
		name    string
		message map[string]any
		key     string
	}{
		{name: "config", message: inworldTranscribeConfigMessage(cfg, ""), key: "transcribeConfig"},
		{name: "audio", message: inworldAudioChunkMessage([]byte{1, 2}), key: "audioChunk"},
		{name: "end turn", message: inworldEndTurnMessage(), key: "endTurn"},
		{name: "close stream", message: inworldCloseStreamMessage(), key: "closeStream"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, ok := test.message[test.key]; !ok {
				t.Fatalf("message %#v is missing %q", test.message, test.key)
			}
			raw, err := json.Marshal(test.message)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(raw), "transcribe_config") ||
				strings.Contains(string(raw), "audio_chunk") ||
				strings.Contains(string(raw), "end_turn") ||
				strings.Contains(string(raw), "close_stream") {
				t.Fatalf("message uses an unsupported snake_case envelope: %s", raw)
			}
		})
	}
}
