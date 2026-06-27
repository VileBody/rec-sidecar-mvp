package clean

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

const instrumentationName = "github.com/VileBody/rec-sidecar-mvp/clean_start"

var cleanTracer = otel.Tracer(instrumentationName)

type eventTraceCarrier struct {
	event *Event
}

func (c eventTraceCarrier) Get(key string) string {
	if c.event == nil {
		return ""
	}
	switch strings.ToLower(key) {
	case "traceparent":
		return c.event.TraceParent
	default:
		return ""
	}
}

func (c eventTraceCarrier) Set(key, value string) {
	if c.event == nil {
		return
	}
	switch strings.ToLower(key) {
	case "traceparent":
		c.event.TraceParent = value
	}
}

func (c eventTraceCarrier) Keys() []string {
	return []string{"traceparent"}
}

func InitTelemetry(ctx context.Context, cfg Config, logger *slog.Logger) (func(context.Context) error, error) {
	otel.SetTextMapPropagator(propagation.TraceContext{})
	endpoint := otlpGRPCEndpointFromEnv()
	if endpoint == "" {
		logger.Info("otel traces disabled; OTEL_EXPORTER_OTLP_ENDPOINT is empty", "service", cfg.OTelServiceName)
		return func(context.Context) error { return nil }, nil
	}

	exporter, err := otlptracegrpc.New(
		ctx,
		otlptracegrpc.WithEndpoint(endpoint),
		otlptracegrpc.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("create otlp trace exporter: %w", err)
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(cfg.OTelServiceName),
			attribute.String("deployment.environment", cfg.DeployEnv),
			attribute.String("service.version", cfg.GitSHA),
			attribute.String("clean_start.role", cfg.Role),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("create otel resource: %w", err)
	}

	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(provider)
	cleanTracer = provider.Tracer(instrumentationName)
	logger.Info("otel traces enabled", "service", cfg.OTelServiceName, "env", cfg.DeployEnv, "endpoint", endpoint)
	return provider.Shutdown, nil
}

func otlpGRPCEndpointFromEnv() string {
	endpoint := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
	if endpoint == "" {
		endpoint = strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
	}
	return normalizeOTLPGRPCEndpoint(endpoint)
}

func normalizeOTLPGRPCEndpoint(endpoint string) string {
	endpoint = strings.TrimSpace(endpoint)
	if endpoint == "" {
		return ""
	}
	if parsed, err := url.Parse(endpoint); err == nil && parsed.Scheme != "" && parsed.Host != "" {
		return parsed.Host
	}
	return strings.TrimPrefix(strings.TrimPrefix(endpoint, "http://"), "https://")
}

func InstrumentHTTP(name string, handler http.Handler) http.Handler {
	return otelhttp.NewHandler(handler, name)
}

func NewEventFromContext(ctx context.Context, sessionID, typ, source string, data any) Event {
	return EventWithTraceContext(ctx, NewEvent(sessionID, typ, source, data))
}

func EventWithTraceContext(ctx context.Context, event Event) Event {
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return event
	}
	event.TraceID = sc.TraceID().String()
	event.SpanID = sc.SpanID().String()
	otel.GetTextMapPropagator().Inject(ctx, eventTraceCarrier{event: &event})
	return event
}

func ContextFromEvent(parent context.Context, event Event) context.Context {
	if event.TraceParent == "" {
		return parent
	}
	return otel.GetTextMapPropagator().Extract(parent, eventTraceCarrier{event: &event})
}

type traceHeaderGetter interface {
	Get(string) string
}

func EventWithNATSHeaders(event Event, headers traceHeaderGetter) Event {
	if headers == nil {
		return event
	}
	if event.TraceParent == "" {
		event.TraceParent = headers.Get("traceparent")
	}
	if event.TraceID == "" {
		event.TraceID = headers.Get("x-trace-id")
	}
	return event
}

func StartEventSpan(parent context.Context, event Event, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	ctx := ContextFromEvent(parent, event)
	base := []attribute.KeyValue{
		attribute.String("event.id", event.ID),
		attribute.String("event.type", event.Type),
		attribute.String("event.source", event.Source),
		attribute.String("session.id_hash", shortHash(event.SessionID)),
	}
	if event.GenerationID != "" {
		base = append(base, attribute.String("generation.id_hash", shortHash(event.GenerationID)))
	}
	return cleanTracer.Start(ctx, name, trace.WithAttributes(append(base, attrs...)...))
}

func StartSpan(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	return cleanTracer.Start(ctx, name, trace.WithAttributes(attrs...))
}

func InjectTraceHeaders(ctx context.Context, req *http.Request) {
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
}

func traceIDFromContext(ctx context.Context) string {
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return ""
	}
	return sc.TraceID().String()
}

func EndSpan(span trace.Span, err error) {
	if span == nil {
		return
	}
	if err != nil && !errors.Is(err, context.Canceled) {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
}

func ObserveSpanDuration(name string, started time.Time, labels map[string]string) {
	ObserveHistogram(name+"_duration_ms", float64(time.Since(started).Milliseconds()), labels)
}
