package clean

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestSubjectAndParseSubject(t *testing.T) {
	subject := Subject(".clean.session.", "sess-1", EventSellerDone)
	if subject != "clean.session.sess-1.seller.done" {
		t.Fatalf("subject = %q", subject)
	}
	if wildcard := SubjectWildcard(".clean.session."); wildcard != "clean.session.*.>" {
		t.Fatalf("wildcard = %q", wildcard)
	}
	if typed := SubjectTypeWildcard(".clean.session.", EventStageCommitted); typed != "clean.session.*.stage.committed" {
		t.Fatalf("typed wildcard = %q", typed)
	}

	sessionID, typ, ok := ParseSubject(".clean.session.", subject)
	if !ok {
		t.Fatal("expected subject to parse")
	}
	if sessionID != "sess-1" || typ != EventSellerDone {
		t.Fatalf("parsed subject = %q %q, want sess-1 %q", sessionID, typ, EventSellerDone)
	}

	tests := []struct {
		name    string
		subject string
	}{
		{name: "wrong prefix", subject: "other.sess-1.seller.done"},
		{name: "missing type", subject: "clean.session.sess-1"},
		{name: "missing session", subject: "clean.session..seller.done"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, _, ok := ParseSubject("clean.session", tt.subject); ok {
				t.Fatalf("ParseSubject(%q) unexpectedly succeeded", tt.subject)
			}
		})
	}
}

func TestDecodeData(t *testing.T) {
	event := NewEvent("sess-test", EventSellerInput, "test", TextData{Text: "привет"})
	data, err := DecodeData[TextData](event)
	if err != nil {
		t.Fatal(err)
	}
	if data.Text != "привет" {
		t.Fatalf("decoded text = %q", data.Text)
	}

	empty, err := DecodeData[TextData](Event{Type: EventSellerInput})
	if err != nil {
		t.Fatal(err)
	}
	if empty.Text != "" {
		t.Fatalf("empty decode = %#v", empty)
	}

	_, err = DecodeData[TextData](Event{Type: EventSellerInput, Data: json.RawMessage(`{"text":`)})
	if err == nil {
		t.Fatal("expected malformed JSON error")
	}
	if !strings.Contains(err.Error(), EventSellerInput) {
		t.Fatalf("decode error should include event type, got %v", err)
	}
}

func TestNewEventPopulatesEnvelopeAndData(t *testing.T) {
	event := NewEvent("sess-test", EventSellerInput, "unit", TextData{Text: "текст"})

	if !strings.HasPrefix(event.ID, "evt-") {
		t.Fatalf("event id = %q", event.ID)
	}
	if event.SessionID != "sess-test" || event.Type != EventSellerInput || event.Source != "unit" {
		t.Fatalf("unexpected event envelope: %#v", event)
	}
	if event.CreatedAt.IsZero() {
		t.Fatal("CreatedAt should be set")
	}
	if event.CreatedAt.Location().String() != "UTC" {
		t.Fatalf("CreatedAt location = %s, want UTC", event.CreatedAt.Location())
	}

	data, err := DecodeData[TextData](event)
	if err != nil {
		t.Fatal(err)
	}
	if data.Text != "текст" {
		t.Fatalf("event data = %#v", data)
	}
}
