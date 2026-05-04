# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **幻灯片 (Slide Deck) Transformation**: A new feature to transform text content into visually appealing presentation slides.
  - Integration with **Google Gemini 3 Pro Preview** for outline generation.
  - Specialized logic to generate individual images for each slide using **Gemini 3 Pro Image Preview**.
  - Narrative-driven design prompts with full XML style instructions.
  - Interactive slider UI for viewing slides within the application.
- **Infographic Transformation**: A new feature to transform text content into visually appealing infographics.
  - Integration with **Google Gemini Nano Banana** SDK.
  - Support for `gemini-3-pro-image-preview` model for image generation.
  - Expert visualization prompts for hand-drawn/illustration style designs.
  - Full Chinese language support for infographic labels and descriptions.
  - Dedicated UI container for rendering generated infographics in the note view.
- **Mindmap Transformation**: Generate structured visual mindmaps from sources.
  - Integration with **Mermaid.js** for interactive diagram rendering.
  - Automatic extraction of hierarchical concepts and relationships.
- **Enhanced Logging**: Integrated `github.com/kataras/golog` for professional, leveled logging across the application.
- **Configuration**: Added `GOOGLE_API_KEY` environment variable support for Google AI services.
