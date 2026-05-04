package backend

import (
	"strings"
	"testing"

	"github.com/tmc/langchaingo/schema"
)

func TestPackRetrievedContextDeduplicatesAndTruncates(t *testing.T) {
	docs := []schema.Document{
		{
			PageContent: strings.Repeat("alpha ", 120),
			Metadata: map[string]any{
				"source_id": "s1",
				"source":    "one.txt",
			},
		},
		{
			PageContent: strings.Repeat("alpha ", 120), // duplicate content
			Metadata: map[string]any{
				"source_id": "s1",
				"source":    "one.txt",
			},
		},
		{
			PageContent: strings.Repeat("beta ", 120),
			Metadata: map[string]any{
				"source_id": "s2",
				"source":    "two.txt",
			},
		},
	}

	out := packRetrievedContext(docs, 280)
	if out == "" {
		t.Fatalf("expected packed context")
	}
	if len(out) > 1024 {
		t.Fatalf("expected context <= 1024 chars, got %d", len(out))
	}
	if strings.Count(out, "Source ID: s1") > 1 {
		t.Fatalf("expected duplicate source chunk to be deduplicated")
	}
}
