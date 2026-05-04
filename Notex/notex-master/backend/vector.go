package backend

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"unicode"

	"github.com/kataras/golog"
	"github.com/tmc/langchaingo/schema"
)

// VectorStore wraps different vector store implementations
type VectorStore struct {
	cfg  Config
	docs []schema.Document
	mu   sync.RWMutex
	// ingested keeps a fingerprint of ingested source content to prevent
	// duplicate re-ingestion when notebook vectors are lazily loaded.
	ingested map[string]struct{}
}

// VectorStats contains statistics about the vector store
type VectorStats struct {
	TotalDocuments int
	TotalVectors   int
	Dimension      int
}

// NewVectorStore creates a new vector store based on configuration
func NewVectorStore(cfg Config) (*VectorStore, error) {
	// Ensure data directory exists
	if err := os.MkdirAll(filepath.Dir(cfg.SQLitePath), 0755); err != nil {
		return nil, fmt.Errorf("failed to create data directory: %w", err)
	}

	return &VectorStore{
		cfg:      cfg,
		docs:     make([]schema.Document, 0),
		ingested: make(map[string]struct{}),
	}, nil
}

// IngestDocuments loads and indexes documents from file paths
func (vs *VectorStore) IngestDocuments(ctx context.Context, notebookID string, paths []string) error {
	for _, path := range paths {
		fmt.Printf("[VectorStore] Loading file: %s\n", path)

		content, err := vs.ExtractDocument(ctx, path)
		if err != nil {
			return fmt.Errorf("failed to extract document %s: %w", path, err)
		}

		fmt.Printf("[VectorStore] File loaded, size: %d bytes\n", len(content))
		if _, err := vs.IngestSource(ctx, notebookID, filepath.Base(path), filepath.Base(path), content); err != nil {
			return err
		}
	}

	return nil
}

// ExtractDocument reads and converts a document to text/markdown
func (vs *VectorStore) ExtractDocument(ctx context.Context, path string) (string, error) {
	// Check if file needs markitdown conversion
	ext := strings.ToLower(filepath.Ext(path))
	if vs.needsMarkitdown(ext) {
		if vs.cfg.EnableMarkitdown {
			return vs.convertWithMarkitdown(path)
		}
		return "", fmt.Errorf("file type %s requires markitdown conversion; set ENABLE_MARKITDOWN=true and install markitdown", ext)
	}

	// Direct read for text files or when markitdown is disabled
	bytes, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}

	if isLikelyBinary(bytes) {
		return "", fmt.Errorf("uploaded file appears to be binary and cannot be directly parsed as text: %s", filepath.Base(path))
	}

	return string(bytes), nil
}

// IngestText ingests raw text content
func (vs *VectorStore) IngestText(ctx context.Context, notebookID, sourceName, content string) (int, error) {
	return vs.IngestSource(ctx, notebookID, sourceName, sourceName, content)
}

// IngestSource ingests raw text content with explicit source ID and source display name.
func (vs *VectorStore) IngestSource(ctx context.Context, notebookID, sourceID, sourceName, content string) (int, error) {
	fingerprint := buildIngestFingerprint(notebookID, sourceID, sourceName, content)

	if strings.TrimSpace(content) == "" {
		return 0, nil
	}

	// Split content into chunks
	chunks := vs.splitText(content, vs.cfg.ChunkSize, vs.cfg.ChunkOverlap)

	vs.mu.Lock()
	defer vs.mu.Unlock()

	if _, exists := vs.ingested[fingerprint]; exists {
		golog.Infof("[VectorStore] Skipping duplicate ingest for source '%s' in notebook '%s'", sourceName, notebookID)
		return 0, nil
	}

	// Create documents
	for i, chunk := range chunks {
		tokens := tokenizeText(chunk)
		doc := schema.Document{
			PageContent: chunk,
			Metadata: map[string]any{
				"notebook_id": notebookID,
				"source_id":   sourceID,
				"source":      sourceName,
				"chunk":       i,
				"token_count": len(tokens),
				"ingest_key":  fingerprint,
			},
		}
		vs.docs = append(vs.docs, doc)
	}

	vs.ingested[fingerprint] = struct{}{}

	golog.Infof("[VectorStore] Ingested %d chunks from source '%s' (total docs: %d)\n", len(chunks), sourceName, len(vs.docs))
	return len(chunks), nil
}

// splitText splits text into chunks
func (vs *VectorStore) splitText(text string, chunkSize, chunkOverlap int) []string {
	if chunkSize <= 0 {
		chunkSize = 1000
	}
	if chunkOverlap < 0 {
		chunkOverlap = 200
	}

	// Quiet output for large texts
	if len(text) > 10000 {
		fmt.Printf("[VectorStore] Splitting text (len=%d)...\n", len(text))
	} else {
		fmt.Printf("[VectorStore] Splitting text (len=%d, chunkSize=%d, overlap=%d)\n", len(text), chunkSize, chunkOverlap)
	}

	var chunks []string

	// Check if text contains mostly CJK characters (Chinese, Japanese, Korean)
	runes := []rune(text)
	cjkCount := 0
	sampleSize := 1000
	if len(runes) < sampleSize {
		sampleSize = len(runes)
	}
	for i := 0; i < sampleSize; i++ {
		r := runes[i]
		if r >= 0x4E00 && r <= 0x9FFF { // CJK Unified Ideographs
			cjkCount++
		}
	}
	cjkRatio := float64(cjkCount) / float64(sampleSize)

	if cjkRatio > 0.3 {
		// For CJK text, split by character count (runes)
		// fmt.Println("[VectorStore] Using CJK splitting (by character count)")
		for i := 0; i < len(runes); i += (chunkSize - chunkOverlap) {
			end := i + chunkSize
			if end > len(runes) {
				end = len(runes)
			}

			chunk := string(runes[i:end])
			chunks = append(chunks, chunk)

			if end >= len(runes) {
				break
			}
		}
	} else {
		// For Western text, split by words
		// fmt.Println("[VectorStore] Using word-based splitting")
		words := strings.Fields(text)

		for i := 0; i < len(words); i += (chunkSize - chunkOverlap) {
			end := i + chunkSize
			if end > len(words) {
				end = len(words)
			}

			chunk := strings.Join(words[i:end], " ")
			chunks = append(chunks, chunk)

			if end >= len(words) {
				break
			}
		}
	}

	// fmt.Printf("[VectorStore] Created %d chunks\n", len(chunks))
	return chunks
}

// SimilaritySearch performs a similarity search (simple keyword matching for now)
func (vs *VectorStore) SimilaritySearch(ctx context.Context, notebookID, query string, numDocs int) ([]schema.Document, error) {
	if numDocs <= 0 {
		numDocs = 5
	}

	vs.mu.RLock()
	defer vs.mu.RUnlock()

	// fmt.Printf("[VectorStore] Searching for '%s' in notebook %s (total docs: %d)\n", query, notebookID, len(vs.docs))

	if len(vs.docs) == 0 {
		// fmt.Println("[VectorStore] No documents available for search")
		return []schema.Document{}, nil
	}

	// Filter docs by notebookID
	candidateDocs := make([]schema.Document, 0)
	for _, doc := range vs.docs {
		if nid, ok := doc.Metadata["notebook_id"].(string); ok && nid == notebookID {
			candidateDocs = append(candidateDocs, doc)
		}
	}

	if len(candidateDocs) == 0 {
		return []schema.Document{}, nil
	}

	queryLower := strings.ToLower(query)
	queryTokens := tokenizeText(queryLower)
	queryRunes := []rune(queryLower)
	queryTrigrams := charNgrams(queryLower, 3)

	type docScore struct {
		doc     schema.Document
		bm25    float64
		fuzzy   float64
		lexical float64
		hybrid  float64
	}

	// Precompute BM25 statistics on candidate docs.
	docTokens := make([][]string, len(candidateDocs))
	docTFs := make([]map[string]int, len(candidateDocs))
	df := make(map[string]int)
	avgDocLen := 0.0

	for i, doc := range candidateDocs {
		tokens := tokenizeText(strings.ToLower(doc.PageContent))
		docTokens[i] = tokens
		tf := make(map[string]int)
		seen := make(map[string]struct{})
		for _, token := range tokens {
			tf[token]++
			if _, ok := seen[token]; !ok {
				df[token]++
				seen[token] = struct{}{}
			}
		}
		docTFs[i] = tf
		avgDocLen += float64(len(tokens))
	}

	if len(candidateDocs) > 0 {
		avgDocLen /= float64(len(candidateDocs))
	}
	if avgDocLen <= 0 {
		avgDocLen = 1
	}

	scores := make([]docScore, 0, len(candidateDocs))
	for i, doc := range candidateDocs {
		content := strings.ToLower(doc.PageContent)

		bm25Score := bm25(queryTokens, docTokens[i], docTFs[i], df, len(candidateDocs), avgDocLen)

		fuzzyScore := 0.0
		docTrigrams := charNgrams(content, 3)
		fuzzyScore += jaccardSimilarity(queryTrigrams, docTrigrams) * 4.0

		lexicalScore := 0.0
		if strings.Contains(content, queryLower) {
			lexicalScore += 6.0
		}
		if len(queryRunes) > 0 {
			matchCount := 0
			for _, r := range queryRunes {
				if strings.ContainsRune(content, r) {
					matchCount++
				}
			}
			lexicalScore += (float64(matchCount) / float64(len(queryRunes))) * 2.0
		}

		hybridScore := (0.55 * bm25Score) + (0.30 * fuzzyScore) + (0.15 * lexicalScore)
		if hybridScore > 0 {
			scores = append(scores, docScore{
				doc:     doc,
				bm25:    bm25Score,
				fuzzy:   fuzzyScore,
				lexical: lexicalScore,
				hybrid:  hybridScore,
			})
		}
	}

	// fmt.Printf("[VectorStore] Found %d matching documents\n", len(scores))

	// Sort by score descending
	sort.Slice(scores, func(i, j int) bool {
		return scores[i].hybrid > scores[j].hybrid
	})

	// If no matches found, return top recent documents (fallback)
	// This allows the LLM to use the full context
	if len(scores) == 0 {
		// fmt.Println("[VectorStore] No matches found, returning fallback documents")
		result := make([]schema.Document, 0, min(numDocs, len(candidateDocs)))
		// Return from end (most recent)
		for i := len(candidateDocs) - 1; i >= 0 && len(result) < numDocs; i-- {
			result = append(result, candidateDocs[i])
		}
		return result, nil
	}

	// Return top results with source diversity (avoid one long source dominating context)
	result := make([]schema.Document, 0, numDocs)
	perSource := make(map[string]int)
	maxPerSource := max(1, numDocs/2)

	for i := 0; i < len(scores) && len(result) < numDocs; i++ {
		sourceID, _ := scores[i].doc.Metadata["source_id"].(string)
		if sourceID == "" {
			sourceID, _ = scores[i].doc.Metadata["source"].(string)
		}
		if sourceID != "" && perSource[sourceID] >= maxPerSource {
			continue
		}
		result = append(result, scores[i].doc)
		if sourceID != "" {
			perSource[sourceID]++
		}
	}

	// Fill remaining slots if source diversity filter was too strict.
	if len(result) < numDocs {
		seenDocPtrs := make(map[int]struct{})
		for _, doc := range result {
			for idx, scored := range scores {
				if scored.doc.PageContent == doc.PageContent {
					seenDocPtrs[idx] = struct{}{}
					break
				}
			}
		}
		for idx := range scores {
			if len(result) >= numDocs {
				break
			}
			if _, ok := seenDocPtrs[idx]; ok {
				continue
			}
			result = append(result, scores[idx].doc)
		}
	}

	return result, nil
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// Delete removes documents by source
func (vs *VectorStore) Delete(ctx context.Context, notebookID, sourceID, source string) error {
	vs.mu.Lock()
	defer vs.mu.Unlock()

	filtered := make([]schema.Document, 0, len(vs.docs))
	deletedIngestKeys := make(map[string]struct{})
	for _, doc := range vs.docs {
		docSource, hasSource := doc.Metadata["source"].(string)
		docNotebookID, hasNotebook := doc.Metadata["notebook_id"].(string)
		docSourceID, hasSourceID := doc.Metadata["source_id"].(string)

		matchesSource := hasSource && docSource == source
		if sourceID != "" && hasSourceID {
			matchesSource = docSourceID == sourceID
		}

		if !hasNotebook || docNotebookID != notebookID || !matchesSource {
			filtered = append(filtered, doc)
			continue
		}

		if ingestKey, ok := doc.Metadata["ingest_key"].(string); ok {
			deletedIngestKeys[ingestKey] = struct{}{}
		}
	}
	vs.docs = filtered

	for ingestKey := range deletedIngestKeys {
		delete(vs.ingested, ingestKey)
	}

	return nil
}

func buildIngestFingerprint(notebookID, sourceID, sourceName, content string) string {
	hash := sha256.Sum256([]byte(notebookID + "\n" + sourceID + "\n" + sourceName + "\n" + content))
	return hex.EncodeToString(hash[:])
}

func isLikelyBinary(data []byte) bool {
	if len(data) == 0 {
		return false
	}

	if len(data) > 8192 {
		data = data[:8192]
	}

	nonText := 0
	for _, b := range data {
		if b == 0 {
			return true
		}
		if (b < 9) || (b > 13 && b < 32) {
			nonText++
		}
	}

	ratio := float64(nonText) / float64(len(data))
	return ratio > 0.10
}

func tokenizeText(text string) []string {
	if text == "" {
		return nil
	}

	parts := strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		if unicode.IsLetter(r) || unicode.IsNumber(r) {
			return false
		}
		if r >= 0x4E00 && r <= 0x9FFF {
			return false
		}
		return true
	})

	filtered := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		filtered = append(filtered, p)
	}
	return filtered
}

func bm25(queryTokens, docTokens []string, tf map[string]int, df map[string]int, totalDocs int, avgDocLen float64) float64 {
	if len(queryTokens) == 0 || len(docTokens) == 0 || totalDocs == 0 {
		return 0
	}

	k1 := 1.5
	b := 0.75
	docLen := float64(len(docTokens))

	score := 0.0
	seen := make(map[string]struct{})
	for _, token := range queryTokens {
		if _, ok := seen[token]; ok {
			continue
		}
		seen[token] = struct{}{}

		docFreq := float64(df[token])
		if docFreq <= 0 {
			continue
		}

		idf := math.Log(1 + ((float64(totalDocs) - docFreq + 0.5) / (docFreq + 0.5)))
		termFreq := float64(tf[token])
		if termFreq <= 0 {
			continue
		}

		numerator := termFreq * (k1 + 1)
		denominator := termFreq + k1*(1-b+b*(docLen/avgDocLen))
		if denominator <= 0 {
			continue
		}

		score += idf * (numerator / denominator)
	}

	return score
}

func charNgrams(text string, n int) map[string]struct{} {
	runes := []rune(strings.ToLower(strings.TrimSpace(text)))
	grams := make(map[string]struct{})
	if len(runes) == 0 {
		return grams
	}
	if len(runes) < n {
		grams[string(runes)] = struct{}{}
		return grams
	}
	for i := 0; i <= len(runes)-n; i++ {
		grams[string(runes[i:i+n])] = struct{}{}
	}
	return grams
}

func jaccardSimilarity(a, b map[string]struct{}) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}

	intersection := 0
	for k := range a {
		if _, ok := b[k]; ok {
			intersection++
		}
	}
	union := len(a) + len(b) - intersection
	if union <= 0 {
		return 0
	}
	return float64(intersection) / float64(union)
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// GetStats returns statistics about the vector store
func (vs *VectorStore) GetStats(ctx context.Context) (VectorStats, error) {
	vs.mu.RLock()
	defer vs.mu.RUnlock()

	stats := VectorStats{
		TotalDocuments: len(vs.docs),
		Dimension:      1536, // Default for OpenAI embeddings
	}

	if vs.cfg.IsOllama() {
		stats.Dimension = 768 // Common for Ollama models
	}

	return stats, nil
}

// needsMarkitdown checks if a file extension requires markitdown conversion
func (vs *VectorStore) needsMarkitdown(ext string) bool {
	markitdownExts := map[string]bool{
		".pdf":  true,
		".docx": true,
		".doc":  true,
		".pptx": true,
		".ppt":  true,
		".xlsx": true,
		".xls":  true,
	}
	return markitdownExts[ext]
}

// ExtractFromURL fetches and converts content from a URL using markitdown
func (vs *VectorStore) ExtractFromURL(ctx context.Context, url string) (string, error) {
	fmt.Printf("[VectorStore] Fetching content from URL: %s\n", url)

	if !vs.cfg.EnableMarkitdown {
		return "", fmt.Errorf("markitdown is disabled, cannot fetch URL content")
	}

	// Create temporary output file
	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("markitdown_url_%d.md", os.Getpid()))

	// Run markitdown command with URL
	cmd := exec.Command("markitdown", url, "-o", tmpFile)
	output, err := cmd.CombinedOutput()
	if err != nil {
		fmt.Printf("[VectorStore] markitdown error: %s\n", string(output))
		return "", fmt.Errorf("failed to fetch URL content: %w, output: %s", err, string(output))
	}

	// Read the converted markdown content
	content, err := os.ReadFile(tmpFile)
	if err != nil {
		return "", fmt.Errorf("failed to read markitdown output: %w", err)
	}

	// Clean up temporary file
	os.Remove(tmpFile)

	fmt.Printf("[VectorStore] URL content fetched successfully, output size: %d bytes\n", len(content))
	return string(content), nil
}

// convertWithMarkitdown converts a document to Markdown using the markitdown CLI tool
func (vs *VectorStore) convertWithMarkitdown(filePath string) (string, error) {
	fmt.Printf("[VectorStore] Converting with markitdown: %s\n", filePath)

	// Create temporary output file
	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("markitdown_%s.md", filepath.Base(filePath)))

	// Run markitdown command
	cmd := exec.Command("markitdown", filePath, "-o", tmpFile)
	output, err := cmd.CombinedOutput()
	if err != nil {
		fmt.Printf("[VectorStore] markitdown error: %s\n", string(output))
		return "", fmt.Errorf("markitdown conversion failed: %w, output: %s", err, string(output))
	}

	// Read the converted markdown content
	content, err := os.ReadFile(tmpFile)
	if err != nil {
		return "", fmt.Errorf("failed to read markitdown output: %w", err)
	}

	// Clean up temporary file
	os.Remove(tmpFile)

	fmt.Printf("[VectorStore] markitdown conversion successful, output size: %d bytes\n", len(content))
	return string(content), nil
}
