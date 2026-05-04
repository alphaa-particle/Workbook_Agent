package backend

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/tmc/langchaingo/llms"
	ollamallm "github.com/tmc/langchaingo/llms/ollama"
	"github.com/tmc/langchaingo/llms/openai"
	"github.com/tmc/langchaingo/prompts"
	"github.com/tmc/langchaingo/schema"
)

// Agent handles AI operations for generating notes and chat responses
type Agent struct {
	vectorStore *VectorStore
	llm         llms.Model
	cfg         Config
	provider    LLMProvider
}

// NewAgent creates a new agent
func NewAgent(cfg Config, vectorStore *VectorStore) (*Agent, error) {
	llm, err := createLLM(cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to create LLM: %w", err)
	}

	// Select image provider based on config
	var provider LLMProvider
	switch cfg.ImageProvider {
	case "glm":
		if cfg.GLMAPIKey == "" {
			return nil, fmt.Errorf("glm_api_key is required when image_provider is 'glm'")
		}
		provider = NewGLMImageClient(cfg.GLMAPIKey)
	case "zimage":
		if cfg.ZImageAPIKey == "" {
			return nil, fmt.Errorf("zimage_api_key is required when image_provider is 'zimage'")
		}
		provider = NewZImageClient(cfg.ZImageAPIKey)
	case "gemini":
		provider = NewGeminiClient(cfg.GoogleAPIKey, llm)
	default:
		return nil, fmt.Errorf("unknown image provider: %s (supported: gemini, glm, zimage)", cfg.ImageProvider)
	}

	return &Agent{
		vectorStore: vectorStore,
		llm:         llm,
		cfg:         cfg,
		provider:    provider,
	}, nil
}

// createLLM creates an LLM based on configuration
func createLLM(cfg Config) (llms.Model, error) {
	if cfg.IsOllama() {
		return ollamallm.New(
			ollamallm.WithModel(cfg.OllamaModel),
			ollamallm.WithServerURL(cfg.OllamaBaseURL),
		)
	}

	opts := []openai.Option{
		openai.WithToken(cfg.OpenAIAPIKey),
		openai.WithModel(cfg.OpenAIModel),
	}
	if cfg.OpenAIBaseURL != "" {
		opts = append(opts, openai.WithBaseURL(cfg.OpenAIBaseURL))
	}

	return openai.New(opts...)
}

// GenerateTransformation generates a note based on transformation type
func (a *Agent) GenerateTransformation(ctx context.Context, req *TransformationRequest, sources []Source) (*TransformationResponse, error) {
	// Build context from sources
	var sourceContext strings.Builder
	for i, src := range sources {
		sourceContext.WriteString(fmt.Sprintf("\n## Source %d: %s\n", i+1, src.Name))

		// Use MaxContextLength from config, or default to a safe large value if not set (or too small)
		limit := a.cfg.MaxContextLength
		if limit <= 0 {
			limit = 100000 // Default to 100k chars if config is invalid
		}

		if src.Content != "" {
			if len(src.Content) <= limit {
				sourceContext.WriteString(src.Content)
			} else {
				// Truncate content instead of replacing it entirely
				sourceContext.WriteString(src.Content[:limit])
				sourceContext.WriteString(fmt.Sprintf("\n... [Content truncated, total length: %d]", len(src.Content)))
			}
		} else {
			sourceContext.WriteString(fmt.Sprintf("[Source content: %s, type: %s]", src.Name, src.Type))
		}
		sourceContext.WriteString("\n")
	}

	// Build prompt using f-string format (no Go template reserved names issue)
	promptTemplate := getTransformationPrompt(req.Type)

	prompt := prompts.NewPromptTemplate(
		promptTemplate,
		[]string{"sources", "type", "length", "format", "prompt"},
	)
	prompt.TemplateFormat = prompts.TemplateFormatFString

	promptValue, err := prompt.Format(map[string]any{
		"sources": sourceContext.String(),
		"type":    req.Type,
		"length":  req.Length,
		"format":  req.Format,
		"prompt":  req.Prompt,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to format prompt: %w", err)
	}

	// Generate response
	var response string
	var genErr error

	ctx, cancel := context.WithTimeout(ctx, 300*time.Second)
	defer cancel()
	response, genErr = a.provider.GenerateFromSinglePrompt(ctx, a.llm, promptValue)

	if genErr != nil {
		return nil, fmt.Errorf("failed to generate response: %w", genErr)
	}

	// Build source summaries
	sourceSummaries := make([]SourceSummary, len(sources))
	for i, src := range sources {
		sourceSummaries[i] = SourceSummary{
			ID:   src.ID,
			Name: src.Name,
			Type: src.Type,
		}
	}

	return &TransformationResponse{
		Type:      req.Type,
		Content:   response,
		Sources:   sourceSummaries,
		CreatedAt: time.Now(),
		Metadata: map[string]interface{}{
			"length": req.Length,
			"format": req.Format,
		},
	}, nil
}

// Chat performs a chat query with RAG
func (a *Agent) Chat(ctx context.Context, notebookID, message string, history []ChatMessage) (*ChatResponse, error) {
	// Perform similarity search to find relevant sources
	docs, err := a.vectorStore.SimilaritySearch(ctx, notebookID, message, a.cfg.MaxSources)
	if err != nil {
		return nil, fmt.Errorf("failed to search documents: %w", err)
	}

	// Build context from retrieved documents with token-budget aware packing.
	contextLimit := a.cfg.MaxContextLength
	if contextLimit <= 0 {
		contextLimit = 8000
	}
	contextText := packRetrievedContext(docs, contextLimit)

	// Build chat history
	var historyBuilder strings.Builder
	for i, msg := range history {
		if i >= 10 { // Limit history
			break
		}
		role := "User"
		if msg.Role == "assistant" {
			role = "Assistant"
		}
		historyBuilder.WriteString(fmt.Sprintf("%s: %s\n", role, msg.Content))
	}

	// Create RAG prompt using f-string format
	promptTemplate := prompts.NewPromptTemplate(
		chatSystemPrompt(),
		[]string{"history", "context", "question"},
	)
	promptTemplate.TemplateFormat = prompts.TemplateFormatFString

	promptValue, err := promptTemplate.Format(map[string]any{
		"history":  historyBuilder.String(),
		"context":  contextText,
		"question": message,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to format prompt: %w", err)
	}

	// Generate response
	ctx, cancel := context.WithTimeout(ctx, 300*time.Second)
	defer cancel()

	response, err := a.provider.GenerateFromSinglePrompt(ctx, a.llm, promptValue)
	if err != nil {
		return nil, fmt.Errorf("failed to generate response: %w", err)
	}

	// Build source summaries
	sourceSummaries := make([]SourceSummary, 0, len(docs))
	sourceMap := make(map[string]bool)
	sourceOrder := make([]SourceSummary, 0, len(docs))
	for _, doc := range docs {
		sourceName, _ := doc.Metadata["source"].(string)
		sourceID, _ := doc.Metadata["source_id"].(string)
		if sourceID == "" {
			sourceID = sourceName
		}
		if sourceName == "" {
			sourceName = sourceID
		}
		if sourceID == "" {
			continue
		}
		if !sourceMap[sourceID] {
			sourceOrder = append(sourceOrder, SourceSummary{
				ID:   sourceID,
				Name: sourceName,
				Type: "file",
			})
			sourceMap[sourceID] = true
		}
	}

	// Keep deterministic ordering by first appearance then by name for stable UI.
	sourceSummaries = append(sourceSummaries, sourceOrder...)
	sort.SliceStable(sourceSummaries, func(i, j int) bool {
		if sourceSummaries[i].ID == sourceSummaries[j].ID {
			return sourceSummaries[i].Name < sourceSummaries[j].Name
		}
		return false
	})

	return &ChatResponse{
		Message:   response,
		Sources:   sourceSummaries,
		SessionID: notebookID,
		Metadata: map[string]interface{}{
			"docs_retrieved": len(docs),
		},
	}, nil
}

func packRetrievedContext(docs []schema.Document, maxChars int) string {
	if len(docs) == 0 {
		return ""
	}
	if maxChars < 1024 {
		maxChars = 1024
	}

	var builder strings.Builder
	builder.WriteString("Relevant information from sources:\n\n")

	seen := make(map[string]struct{})
	for i, doc := range docs {
		chunk := strings.TrimSpace(doc.PageContent)
		if chunk == "" {
			continue
		}

		normalized := strings.Join(strings.Fields(strings.ToLower(chunk)), " ")
		if _, ok := seen[normalized]; ok {
			continue
		}
		seen[normalized] = struct{}{}

		sourceName, _ := doc.Metadata["source"].(string)
		sourceID, _ := doc.Metadata["source_id"].(string)

		entry := fmt.Sprintf("[Source %d]\nSource ID: %s\nSource Name: %s\n%s\n\n", i+1, sourceID, sourceName, chunk)
		if builder.Len()+len(entry) > maxChars {
			remaining := maxChars - builder.Len()
			if remaining > 64 {
				entry = entry[:remaining]
				builder.WriteString(entry)
			}
			break
		}
		builder.WriteString(entry)
	}

	return builder.String()
}

// GenerateSummary generates a summary from sources
func (a *Agent) GenerateSummary(ctx context.Context, sources []Source, length string) (string, error) {
	req := &TransformationRequest{
		Type:   "summary",
		Length: length,
		Format: "markdown",
	}

	resp, err := a.GenerateTransformation(ctx, req, sources)
	if err != nil {
		return "", err
	}

	return resp.Content, nil
}
