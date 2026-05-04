package backend

import (
	"context"
	"os"
	"strings"
	"testing"
)

func newTestVectorStore(t *testing.T, cfg Config) *VectorStore {
	t.Helper()
	vs, err := NewVectorStore(cfg)
	if err != nil {
		t.Fatalf("NewVectorStore failed: %v", err)
	}
	return vs
}

func TestExtractDocumentRequiresMarkitdownForPDF(t *testing.T) {
	cfg := Config{
		EnableMarkitdown: false,
		SQLitePath:       "./data/test_vector.db",
	}
	vs := newTestVectorStore(t, cfg)

	pdfPath := t.TempDir() + "/sample.pdf"
	if err := osWriteFile(pdfPath, []byte("%PDF-1.4 fake")); err != nil {
		t.Fatalf("write temp pdf failed: %v", err)
	}

	_, err := vs.ExtractDocument(context.Background(), pdfPath)
	if err == nil {
		t.Fatalf("expected error for pdf without markitdown")
	}
	if !strings.Contains(err.Error(), "requires markitdown") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestExtractDocumentRejectsBinaryDirectRead(t *testing.T) {
	cfg := Config{
		EnableMarkitdown: false,
		SQLitePath:       "./data/test_vector.db",
	}
	vs := newTestVectorStore(t, cfg)

	path := t.TempDir() + "/binary.txt"
	if err := osWriteFile(path, []byte{0x00, 0x01, 0x02, 0x03, 0x04}); err != nil {
		t.Fatalf("write temp file failed: %v", err)
	}

	_, err := vs.ExtractDocument(context.Background(), path)
	if err == nil {
		t.Fatalf("expected binary detection error")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "binary") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestIngestSourceDeduplicatesSameContent(t *testing.T) {
	cfg := Config{SQLitePath: "./data/test_vector.db", ChunkSize: 1000, ChunkOverlap: 200}
	vs := newTestVectorStore(t, cfg)

	ctx := context.Background()
	text := "alpha beta gamma delta"

	count1, err := vs.IngestSource(ctx, "nb1", "src1", "doc1.txt", text)
	if err != nil {
		t.Fatalf("first ingest failed: %v", err)
	}
	if count1 == 0 {
		t.Fatalf("expected chunks from first ingest")
	}
	before := len(vs.docs)

	count2, err := vs.IngestSource(ctx, "nb1", "src1", "doc1.txt", text)
	if err != nil {
		t.Fatalf("second ingest failed: %v", err)
	}
	if count2 != 0 {
		t.Fatalf("expected dedup second ingest to return 0, got %d", count2)
	}
	if len(vs.docs) != before {
		t.Fatalf("expected docs unchanged after duplicate ingest, before=%d after=%d", before, len(vs.docs))
	}
}

func TestDeleteRemovesOnlyTargetSource(t *testing.T) {
	cfg := Config{SQLitePath: "./data/test_vector.db", ChunkSize: 1000, ChunkOverlap: 200}
	vs := newTestVectorStore(t, cfg)
	ctx := context.Background()

	_, _ = vs.IngestSource(ctx, "nb1", "srcA", "shared-name.txt", "content from source A")
	_, _ = vs.IngestSource(ctx, "nb1", "srcB", "shared-name.txt", "content from source B")

	if len(vs.docs) < 2 {
		t.Fatalf("expected at least two docs, got %d", len(vs.docs))
	}

	if err := vs.Delete(ctx, "nb1", "srcA", "shared-name.txt"); err != nil {
		t.Fatalf("delete failed: %v", err)
	}

	for _, doc := range vs.docs {
		sid, _ := doc.Metadata["source_id"].(string)
		if sid == "srcA" {
			t.Fatalf("source srcA should have been deleted")
		}
	}

	foundSrcB := false
	for _, doc := range vs.docs {
		sid, _ := doc.Metadata["source_id"].(string)
		if sid == "srcB" {
			foundSrcB = true
		}
	}
	if !foundSrcB {
		t.Fatalf("expected srcB docs to remain")
	}
}

func osWriteFile(path string, data []byte) error {
	return os.WriteFile(path, data, 0644)
}
