package clean

import (
	"encoding/json"
	"testing"
)

func TestScorecardFromStageUsesRawLLMScorecard(t *testing.T) {
	raw := json.RawMessage(`{"readiness":"green","checks":[]}`)
	scorecard := scorecardFromStage(StageData{
		Step:      "Переходить к питчу.",
		Scorecard: raw,
	})

	if scorecard.Source != "llm-helper" || scorecard.Readiness != "pending" {
		t.Fatalf("unexpected scorecard: %#v", scorecard)
	}
	if string(scorecard.Raw) != string(raw) {
		t.Fatalf("raw scorecard = %s, want %s", scorecard.Raw, raw)
	}
	if scorecard.NextAction != "Переходить к питчу." {
		t.Fatalf("next action = %q", scorecard.NextAction)
	}
}

func TestScorecardFromStageFallsBackWithoutRaw(t *testing.T) {
	scorecard := scorecardFromStage(StageData{Step: "Уточнить боль."})

	if scorecard.Source != "heuristic" || scorecard.Readiness != "yellow" {
		t.Fatalf("unexpected fallback scorecard: %#v", scorecard)
	}
	if scorecard.Raw != nil {
		t.Fatalf("fallback raw should be nil, got %s", scorecard.Raw)
	}
	if scorecard.NextAction != "Уточнить боль." {
		t.Fatalf("next action = %q", scorecard.NextAction)
	}
}
