# Image Generation Plugin for gptme

Multi-provider image generation for gptme.

## Overview

The image generation plugin provides a unified interface for generating images from text descriptions across multiple providers including Google Gemini (Imagen), OpenAI DALL-E, and more.

## Features

- **Multi-provider support**: Gemini, DALL-E 3, DALL-E 2
- **Unified interface**: Same API across all providers
- **Automatic file handling**: Images saved to disk with metadata
- **Quality options**: Choose between standard and HD quality
- **Flexible sizing**: Provider-specific size options

## Installation

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/plugins"]
enabled = ["gptme_image_gen"]
```

Set up API keys:
```bash
export GOOGLE_API_KEY="your-key"  # For Gemini
export OPENAI_API_KEY="your-key"  # For DALL-E
```

## Usage

### Basic Generation

```image_gen
generate_image(
    prompt="A modern office workspace with clean design",
    provider="gemini"
)
```

### With Custom Output Path

```image_gen
generate_image(
    prompt="Architecture diagram of microservices",
    provider="gemini",
    output_path="diagrams/architecture.png"
)
```

### High Quality DALL-E

```image_gen
generate_image(
    prompt="Professional logo design for tech startup",
    provider="dalle",
    quality="hd",
    output_path="branding/logo.png"
)
```

## Providers

### Gemini (Imagen 3)
- **Model**: imagen-3-fast-generate-001
- **Best for**: Fast, high-quality generations
- **Requires**: GOOGLE_API_KEY

### DALL-E 3
- **Model**: dall-e-3
- **Best for**: Creative, detailed images
- **Quality**: standard, hd
- **Requires**: OPENAI_API_KEY

### DALL-E 2
- **Model**: dall-e-2
- **Best for**: Faster, lower cost
- **Requires**: OPENAI_API_KEY

## Parameters

- `prompt` (required): Text description of image
- `provider` (optional): "gemini", "dalle", or "dalle2" (default: "gemini")
- `size` (optional): Image size like "1024x1024" (default: "1024x1024")
- `quality` (optional): "standard" or "hd" (default: "standard")
- `output_path` (optional): Save location (default: auto-generated)

## Use Cases

- **Technical Diagrams**: Architecture, flow charts, system diagrams
- **UI Mockups**: Interface designs, wireframes
- **Presentations**: Illustrations, graphics, slides
- **Documentation**: Visual aids, examples
- **Branding**: Logos, icons, graphics
- **Concept Art**: Prototypes, visual exploration

## Output

The tool returns:
- **Provider**: Which service generated the image
- **Prompt**: Original text description
- **Image Path**: Where the image was saved
- **Metadata**: Model, size, quality details

## Dependencies

Required:
```bash
pip install google-generativeai  # For Gemini
pip install openai               # For DALL-E
pip install requests             # For image downloads
```

## Future Enhancements

- [ ] Local Stable Diffusion support
- [ ] Batch generation
- [ ] Style presets
- [ ] Image editing/inpainting
- [ ] Aspect ratio templates
- [ ] Cost tracking per provider
