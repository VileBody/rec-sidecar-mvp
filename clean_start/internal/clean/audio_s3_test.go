package clean

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAudioRecorderUploadsWAVToS3(t *testing.T) {
	var gotPath string
	var gotAuth string
	var gotContentType string
	var gotBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		gotContentType = r.Header.Get("Content-Type")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	sink := NewAudioSink(Config{
		AudioS3Endpoint:  server.URL,
		AudioS3Region:    "test-region",
		AudioS3Bucket:    "rec-bucket",
		AudioS3AccessKey: "access",
		AudioS3SecretKey: "secret",
		AudioS3Prefix:    "calls",
		AudioS3PathStyle: true,
	}, noopLogger())
	recorder := sink.Start("sess-test", "client", "browser system")
	if recorder == nil {
		t.Fatal("recorder is nil")
	}
	if err := recorder.WritePCM([]byte{0, 0, 1, 0, 2, 0, 3, 0}); err != nil {
		t.Fatal(err)
	}
	if err := recorder.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	if !strings.HasPrefix(gotPath, "/rec-bucket/calls/sess-test/") || !strings.HasSuffix(gotPath, ".wav") {
		t.Fatalf("path = %q", gotPath)
	}
	if gotContentType != "audio/wav" {
		t.Fatalf("content-type = %q", gotContentType)
	}
	if !strings.HasPrefix(gotAuth, "AWS4-HMAC-SHA256 Credential=access/") {
		t.Fatalf("authorization = %q", gotAuth)
	}
	if !bytes.HasPrefix(gotBody, []byte("RIFF")) || !bytes.Contains(gotBody[:44], []byte("WAVE")) {
		t.Fatalf("uploaded body is not wav: %q", string(gotBody[:minInt(len(gotBody), 16)]))
	}
}
