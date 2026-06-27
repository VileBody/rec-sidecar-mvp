package clean

import "testing"

func TestNormalizeOTLPGRPCEndpoint(t *testing.T) {
	cases := map[string]string{
		"http://otel-collector:4317": "otel-collector:4317",
		"https://tempo.local:4317":   "tempo.local:4317",
		"otel-collector:4317":        "otel-collector:4317",
		"  http://127.0.0.1:4317  ":  "127.0.0.1:4317",
		"http://[::1]:4317":          "[::1]:4317",
		"":                           "",
		"  ":                         "",
	}
	for input, want := range cases {
		if got := normalizeOTLPGRPCEndpoint(input); got != want {
			t.Fatalf("normalizeOTLPGRPCEndpoint(%q)=%q, want %q", input, got, want)
		}
	}
}
