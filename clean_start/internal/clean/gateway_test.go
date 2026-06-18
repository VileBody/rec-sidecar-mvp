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
