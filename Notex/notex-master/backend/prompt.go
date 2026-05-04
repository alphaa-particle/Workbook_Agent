package backend

// getTransformationPrompt returns the prompt template for each transformation type
func getTransformationPrompt(transformType string) string {
	switch transformType {
	case "summary":
		return summaryPrompt()

	case "custom":
		return customPrompt()

	default:
		return defaultPrompt()
	}
}

func summaryPrompt() string {
	return "You are an expert at creating comprehensive summaries. Based on the following sources, create a {length} summary in {format} format.\n" +
		"**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n" +
		"Sources:\n{sources}\n\n" +
		"Provide a well-structured summary that captures the key information, main topics, and important details from the sources."
}

func customPrompt() string {
	return "You are a helpful assistant. Based on the following sources and custom request, generate the requested content.\n" +
		"**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n" +
		"Sources:\n{sources}\n\n" +
		"Custom request:\n{prompt}\n\n" +
		"Please generate the content in {format} format, keeping it {length}."
}

func defaultPrompt() string {
	return "You are a helpful assistant. Based on the following sources, provide a {type} in {format} format.\n" +
		"**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n" +
		"Sources:\n{sources}\n\n" +
		"Generate {length} content."
}

// Chat system prompt
func chatSystemPrompt() string {
	return "You are a helpful AI assistant for a notebook application. Answer the user's questions based on the provided context and chat history.\n" +
		"**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n" +
		"If there is not enough information in the context, state this and provide a general answer.\n\n" +
		"Chat history:\n{history}\n\n" +
		"Context:\n{context}\n\n" +
		"User question: {question}\n\n" +
		"Provide a helpful and accurate answer. When referencing information from the sources, mention which source the information comes from."
}
