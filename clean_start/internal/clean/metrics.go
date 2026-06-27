package clean

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

var defaultHistogramBuckets = []float64{50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000}

type metricSample struct {
	typ    string
	help   string
	value  float64
	counts []uint64
	sum    float64
}

type metricsRegistry struct {
	mu      sync.Mutex
	samples map[string]*metricSample
}

var appMetrics = &metricsRegistry{samples: make(map[string]*metricSample)}

func IncCounter(name string, labels map[string]string) {
	appMetrics.mu.Lock()
	defer appMetrics.mu.Unlock()
	key := metricKey(name, labels)
	sample := appMetrics.samples[key]
	if sample == nil {
		sample = &metricSample{typ: "counter", help: name + " counter"}
		appMetrics.samples[key] = sample
	}
	sample.value++
}

func AddCounter(name string, value float64, labels map[string]string) {
	if value <= 0 {
		return
	}
	appMetrics.mu.Lock()
	defer appMetrics.mu.Unlock()
	key := metricKey(name, labels)
	sample := appMetrics.samples[key]
	if sample == nil {
		sample = &metricSample{typ: "counter", help: name + " counter"}
		appMetrics.samples[key] = sample
	}
	sample.value += value
}

func SetGauge(name string, value float64, labels map[string]string) {
	appMetrics.mu.Lock()
	defer appMetrics.mu.Unlock()
	key := metricKey(name, labels)
	sample := appMetrics.samples[key]
	if sample == nil {
		sample = &metricSample{typ: "gauge", help: name + " gauge"}
		appMetrics.samples[key] = sample
	}
	sample.value = value
}

func ObserveHistogram(name string, value float64, labels map[string]string) {
	if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return
	}
	appMetrics.mu.Lock()
	defer appMetrics.mu.Unlock()
	key := metricKey(name, labels)
	sample := appMetrics.samples[key]
	if sample == nil {
		sample = &metricSample{typ: "histogram", help: name + " histogram", counts: make([]uint64, len(defaultHistogramBuckets)+1)}
		appMetrics.samples[key] = sample
	}
	for i, bucket := range defaultHistogramBuckets {
		if value <= bucket {
			sample.counts[i]++
		}
	}
	sample.counts[len(defaultHistogramBuckets)]++
	sample.sum += value
}

func MetricsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		appMetrics.mu.Lock()
		defer appMetrics.mu.Unlock()
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		keys := make([]string, 0, len(appMetrics.samples))
		for key := range appMetrics.samples {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		seen := map[string]bool{}
		for _, key := range keys {
			name, labels := splitMetricKey(key)
			sample := appMetrics.samples[key]
			if !seen[name] {
				seen[name] = true
				fmt.Fprintf(w, "# HELP %s %s\n", name, sample.help)
				fmt.Fprintf(w, "# TYPE %s %s\n", name, sample.typ)
			}
			switch sample.typ {
			case "histogram":
				for i, bucket := range defaultHistogramBuckets {
					fmt.Fprintf(w, "%s_bucket%s %d\n", name, appendMetricLabel(labels, "le", fmt.Sprintf("%.0f", bucket)), sample.counts[i])
				}
				fmt.Fprintf(w, "%s_bucket%s %d\n", name, appendMetricLabel(labels, "le", "+Inf"), sample.counts[len(defaultHistogramBuckets)])
				fmt.Fprintf(w, "%s_sum%s %.3f\n", name, labels, sample.sum)
				fmt.Fprintf(w, "%s_count%s %d\n", name, labels, sample.counts[len(defaultHistogramBuckets)])
			default:
				fmt.Fprintf(w, "%s%s %.3f\n", name, labels, sample.value)
			}
		}
	})
}

func StartMetricsServer(ctx context.Context, cfg Config, logger *slog.Logger) func(context.Context) error {
	if strings.TrimSpace(cfg.MetricsAddr) == "" {
		return func(context.Context) error { return nil }
	}
	mux := http.NewServeMux()
	mux.Handle("/metrics", MetricsHandler())
	server := &http.Server{Addr: cfg.MetricsAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()
	go func() {
		logger.Info("metrics listening", "addr", cfg.MetricsAddr)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Warn("metrics server stopped", "error", err)
		}
	}()
	return server.Shutdown
}

func metricKey(name string, labels map[string]string) string {
	return name + normalizedMetricLabels(labels)
}

func splitMetricKey(key string) (string, string) {
	if idx := strings.IndexByte(key, '{'); idx >= 0 {
		return key[:idx], key[idx:]
	}
	return key, ""
}

func normalizedMetricLabels(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for key, value := range labels {
		if strings.TrimSpace(key) == "" || strings.TrimSpace(value) == "" {
			continue
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteByte('{')
	for i, key := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, `%s="%s"`, sanitizeMetricName(key), sanitizeMetricLabelValue(labels[key]))
	}
	b.WriteByte('}')
	return b.String()
}

func appendMetricLabel(labels, key, value string) string {
	added := fmt.Sprintf(`%s="%s"`, sanitizeMetricName(key), sanitizeMetricLabelValue(value))
	if labels == "" {
		return "{" + added + "}"
	}
	return strings.TrimSuffix(labels, "}") + "," + added + "}"
}

func sanitizeMetricName(value string) string {
	var b strings.Builder
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' {
			b.WriteRune(r)
		}
	}
	if b.Len() == 0 {
		return "label"
	}
	return b.String()
}

func sanitizeMetricLabelValue(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, `"`, `\"`)
	value = strings.ReplaceAll(value, "\n", " ")
	return value
}

func shortHash(value string) string {
	if len(value) <= 12 {
		return value
	}
	return value[:6] + value[len(value)-6:]
}
