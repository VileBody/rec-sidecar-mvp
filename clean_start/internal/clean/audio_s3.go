package clean

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

const s3ServiceName = "s3"

var audioKeyUnsafe = regexp.MustCompile(`[^a-zA-Z0-9._/-]+`)

type AudioSink struct {
	cfg      Config
	logger   *slog.Logger
	endpoint *url.URL
	client   *http.Client
}

type AudioRecorder struct {
	sink      *AudioSink
	sessionID string
	role      string
	source    string
	startedAt time.Time
	path      string
	file      *os.File
	bytes     int64
	mu        sync.Mutex
	once      sync.Once
	closed    bool
	err       error
}

func NewAudioSink(cfg Config, logger *slog.Logger) *AudioSink {
	if strings.TrimSpace(cfg.AudioS3Endpoint) == "" {
		return nil
	}
	endpoint, err := parseS3Endpoint(cfg.AudioS3Endpoint)
	if err != nil {
		logger.Warn("audio s3 disabled: bad endpoint", "endpoint", cfg.AudioS3Endpoint, "error", err)
		return nil
	}
	return &AudioSink{
		cfg:      cfg,
		logger:   logger,
		endpoint: endpoint,
		client: &http.Client{
			Timeout: 60 * time.Second,
		},
	}
}

func (s *AudioSink) Configured() bool {
	return s != nil &&
		s.endpoint != nil &&
		strings.TrimSpace(s.cfg.AudioS3Bucket) != "" &&
		strings.TrimSpace(s.cfg.AudioS3AccessKey) != "" &&
		strings.TrimSpace(s.cfg.AudioS3SecretKey) != ""
}

func (s *AudioSink) Start(sessionID, role, source string) *AudioRecorder {
	if !s.Configured() {
		return nil
	}
	file, err := os.CreateTemp("", "rec-clean-start-*.pcm")
	if err != nil {
		s.logger.Warn("audio recording temp file failed", "session_id", sessionID, "role", role, "source", source, "error", err)
		return nil
	}
	recorder := &AudioRecorder{
		sink:      s,
		sessionID: sessionID,
		role:      role,
		source:    source,
		startedAt: time.Now().UTC(),
		path:      file.Name(),
		file:      file,
	}
	s.logger.Info("audio recording started", "session_id", sessionID, "role", role, "source", source, "path", recorder.path)
	return recorder
}

func (s *AudioSink) RecordPCMAsync(sessionID, role, source string, pcm []byte) {
	recorder := s.Start(sessionID, role, source)
	if recorder == nil {
		return
	}
	if err := recorder.WritePCM(pcm); err != nil {
		s.logger.Warn("audio recording write failed", "session_id", sessionID, "role", role, "source", source, "error", err)
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		if err := recorder.Close(ctx); err != nil {
			s.logger.Warn("audio recording upload failed", "session_id", sessionID, "role", role, "source", source, "error", err)
		}
	}()
}

func (r *AudioRecorder) WritePCM(pcm []byte) error {
	if r == nil || len(pcm) == 0 {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil
	}
	n, err := r.file.Write(pcm)
	r.bytes += int64(n)
	return err
}

func (r *AudioRecorder) Close(ctx context.Context) error {
	if r == nil {
		return nil
	}
	r.once.Do(func() {
		r.err = r.close(ctx)
	})
	return r.err
}

func (r *AudioRecorder) close(ctx context.Context) error {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return nil
	}
	r.closed = true
	path := r.path
	size := r.bytes
	file := r.file
	r.mu.Unlock()

	if file != nil {
		if err := file.Close(); err != nil {
			return err
		}
	}
	defer func() { _ = os.Remove(path) }()

	if size == 0 {
		r.sink.logger.Info("audio recording skipped empty file", "session_id", r.sessionID, "role", r.role, "source", r.source)
		return nil
	}
	pcm, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	key := r.sink.objectKey(r.sessionID, r.role, r.source, r.startedAt)
	if err := r.sink.putObject(ctx, key, pcmToWAV(pcm), "audio/wav"); err != nil {
		return err
	}
	r.sink.logger.Info("audio recording uploaded", "session_id", r.sessionID, "role", r.role, "source", r.source, "key", key, "pcm_bytes", len(pcm))
	return nil
}

func (s *AudioSink) objectKey(sessionID, role, source string, at time.Time) string {
	prefix := strings.Trim(s.cfg.AudioS3Prefix, "/")
	datePath := at.UTC().Format("2006/01/02")
	name := strings.Join([]string{
		at.UTC().Format("150405.000"),
		cleanAudioKeyPart(role),
		cleanAudioKeyPart(source),
		NewID("aud"),
	}, "-") + ".wav"
	parts := []string{prefix, cleanAudioKeyPart(sessionID), datePath, name}
	var cleanParts []string
	for _, part := range parts {
		part = strings.Trim(part, "/")
		if part != "" {
			cleanParts = append(cleanParts, part)
		}
	}
	return strings.Join(cleanParts, "/")
}

func cleanAudioKeyPart(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "unknown"
	}
	value = strings.ReplaceAll(value, " ", "-")
	value = strings.ReplaceAll(value, "/", "-")
	value = audioKeyUnsafe.ReplaceAllString(value, "-")
	value = strings.Trim(value, "-/")
	if value == "" {
		return "unknown"
	}
	return value
}

func parseS3Endpoint(raw string) (*url.URL, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("empty endpoint")
	}
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return nil, err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, fmt.Errorf("unsupported endpoint scheme %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return nil, errors.New("missing endpoint host")
	}
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed, nil
}

func (s *AudioSink) putObject(ctx context.Context, key string, body []byte, contentType string) error {
	if !s.Configured() {
		return nil
	}
	objectURL := s.objectURL(key)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, objectURL.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.ContentLength = int64(len(body))
	req.Header.Set("Content-Type", contentType)
	payloadHash := sha256Hex(body)
	req.Header.Set("X-Amz-Content-Sha256", payloadHash)
	now := time.Now().UTC()
	req.Header.Set("X-Amz-Date", now.Format("20060102T150405Z"))
	s.signV4(req, payloadHash, now)

	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("s3 put object http %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return nil
}

func (s *AudioSink) objectURL(key string) url.URL {
	u := *s.endpoint
	basePath := strings.TrimRight(u.Path, "/")
	bucket := strings.Trim(s.cfg.AudioS3Bucket, "/")
	if s.cfg.AudioS3PathStyle {
		u.Path = basePath + "/" + bucket + "/" + strings.TrimLeft(key, "/")
		return u
	}
	u.Host = bucket + "." + u.Host
	u.Path = basePath + "/" + strings.TrimLeft(key, "/")
	return u
}

func (s *AudioSink) signV4(req *http.Request, payloadHash string, at time.Time) {
	date := at.Format("20060102")
	amzDate := at.Format("20060102T150405Z")
	region := strings.TrimSpace(s.cfg.AudioS3Region)
	if region == "" {
		region = "ru-1"
	}
	credentialScope := strings.Join([]string{date, region, s3ServiceName, "aws4_request"}, "/")
	signedHeaders := "content-type;host;x-amz-content-sha256;x-amz-date"
	canonicalHeaders := strings.Join([]string{
		"content-type:" + req.Header.Get("Content-Type"),
		"host:" + req.URL.Host,
		"x-amz-content-sha256:" + payloadHash,
		"x-amz-date:" + amzDate,
		"",
	}, "\n")
	canonicalRequest := strings.Join([]string{
		req.Method,
		req.URL.EscapedPath(),
		req.URL.RawQuery,
		canonicalHeaders,
		signedHeaders,
		payloadHash,
	}, "\n")
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256",
		amzDate,
		credentialScope,
		sha256Hex([]byte(canonicalRequest)),
	}, "\n")
	signature := hex.EncodeToString(hmacSHA256(signingKey(s.cfg.AudioS3SecretKey, date, region), []byte(stringToSign)))
	req.Header.Set("Authorization", fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		s.cfg.AudioS3AccessKey,
		credentialScope,
		signedHeaders,
		signature,
	))
}

func signingKey(secret, date, region string) []byte {
	kDate := hmacSHA256([]byte("AWS4"+secret), []byte(date))
	kRegion := hmacSHA256(kDate, []byte(region))
	kService := hmacSHA256(kRegion, []byte(s3ServiceName))
	return hmacSHA256(kService, []byte("aws4_request"))
}

func hmacSHA256(key, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write(data)
	return mac.Sum(nil)
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
