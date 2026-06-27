package clean

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/trace"
)

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

func TestEventWithTraceContextInjectsTraceParent(t *testing.T) {
	previousPropagator := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer otel.SetTextMapPropagator(previousPropagator)

	provider := trace.NewTracerProvider()
	tracer := provider.Tracer("clean-start-test")
	ctx, span := tracer.Start(context.Background(), "root")
	defer span.End()

	event := EventWithTraceContext(ctx, NewEvent("sess-test", EventSellerInput, "test", TextData{Text: "hello"}))
	if event.TraceID == "" {
		t.Fatal("TraceID should be populated from active span")
	}
	if event.SpanID == "" {
		t.Fatal("SpanID should be populated from active span")
	}
	if event.TraceParent == "" {
		t.Fatal("TraceParent should be injected")
	}
	if !strings.HasPrefix(event.TraceParent, "00-"+event.TraceID+"-"+event.SpanID+"-") {
		t.Fatalf("TraceParent %q does not match trace/span %s/%s", event.TraceParent, event.TraceID, event.SpanID)
	}
}
