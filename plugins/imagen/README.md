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
- **Multiple options generation**: Generate variations for comparison (count parameter) **[Phase 1]**
- **View integration**: Display images to assistant for verification (view parameter) **[Phase 1]**
- **Enhanced error handling**: Clear error messages with context **[Phase 1]**
- **Style presets**: 8 predefined styles for consistent results (style parameter) **[Phase 2]**
- **Prompt enhancement**: Automatic quality and composition improvements (enhance parameter) **[Phase 2]**
- **Progress indicators**: Visual feedback during multi-image generation **[Phase 2]**
- **Cost tracking**: Automatic tracking and reporting of generation costs **[Phase 3]**

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

### Multiple Options (Phase 1 NEW)

Generate multiple variations for comparison:

```image_gen
generate_image(
    prompt="Modern minimalist logo for tech startup",
    provider="gemini",
    count=3,
    output_path="logos/option.png"
)
```

Output: `logos/option_001.png`, `logos/option_002.png`, `logos/option_003.png`

### View Integration (Phase 1 NEW)

Display generated images to the assistant for verification and feedback:

```image_gen
generate_image(
    prompt="UI mockup for dashboard",
    provider="gemini",
    view=True,
    output_path="mockups/dashboard.png"
)
```

The assistant can see the generated image and provide feedback like "The layout looks good, but the colors could be brighter."

### Combined: Multiple Options with View

```image_gen
generate_image(
    prompt="Logo concept with geometric shapes",
    provider="gemini",
    count=3,
    view=True,
    output_path="concepts/logo.png"
)
```

The assistant sees all 3 variations and can recommend the best one.

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
- `count` (optional): Number of variations to generate (default: 1) **[Phase 1 NEW]**
- `view` (optional): Display generated images to assistant (default: False) **[Phase 1 NEW]**

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
pip install google-genai  # For Gemini
pip install openai               # For DALL-E
pip install requests             # For image downloads
```

## Phase 1 Enhancements (Completed)

- [x] Multiple options generation (count parameter)
- [x] View integration (view parameter)
- [x] Enhanced error handling

## Phase 2 Enhancements (Completed)

### Style Presets

Apply predefined style presets to enhance your prompts with consistent artistic direction:

**Available Styles:**
- `photo` - Photorealistic rendering
- `illustration` - Digital illustration style
- `sketch` - Hand-drawn sketch aesthetic
- `technical-diagram` - Clean technical visualization
- `flat-design` - Minimalist flat design
- `cyberpunk` - Futuristic neon aesthetic
- `watercolor` - Traditional watercolor painting
- `oil-painting` - Classic oil painting style

**Usage:**
```image_gen
generate_image(
    prompt="mountain landscape",
    style="watercolor",
    provider="gemini"
)
```

### Prompt Enhancement

Automatically enhance prompts with quality keywords and composition guidance:

**Usage:**
```image_gen
generate_image(
    prompt="cat sitting",
    enhance=True,
    provider="gemini"
)
```

The enhance parameter adds:
- Quality keywords (high quality, detailed, professional)
- Composition guidance for short prompts
- Avoids duplicate keywords already in prompt

**Combined Example:**
```image_gen
generate_image(
    prompt="futuristic city",
    style="cyberpunk",
    enhance=True,
    count=3,
    view=True,
    provider="gemini"
)
```

## Cost Tracking

All image generations are automatically tracked in a local SQLite database (`~/.gptme/imagen_costs.db`).

### Query Total Cost

```ipython
from gptme_image_gen.tools.image_gen import get_total_cost

# Get total cost across all providers
total = get_total_cost()
print(f"Total spent: ${total:.2f}")

# Filter by provider
gemini_cost = get_total_cost(provider="gemini")
print(f"Gemini cost: ${gemini_cost:.2f}")

# Filter by date range
cost = get_total_cost(start_date="2024-11-01", end_date="2024-11-30")
print(f"November cost: ${cost:.2f}")
```

### Cost Breakdown

```ipython
from gptme_image_gen.tools.image_gen import get_cost_breakdown

breakdown = get_cost_breakdown()
for provider, cost in breakdown.items():
    print(f"{provider}: ${cost:.2f}")
```

### Generation History

```ipython
from gptme_image_gen.tools.image_gen import get_generation_history

history = get_generation_history(limit=10)
for gen in history:
    print(f"{gen['timestamp']}: {gen['prompt'][:50]}... (${gen['cost_usd']:.3f})")
```

**Cost per image** (approximate as of Nov 2024):
- Gemini Imagen-3: $0.04 per image (standard)
- DALL-E 3: $0.04 per image (standard), $0.08 per image (HD)
- DALL-E 2: $0.02 per image

**Note**: Costs are tracked automatically with each generation and stored locally.

## Phase 3.2 Enhancements (Completed)

### Image Variations

Generate variations of existing images (DALL-E 2 only):

```image_gen
generate_variation(
    image_path="original.png",
    provider="dalle2",
    count=4,
    view=True
)
```

**Note**: Image variations are currently only supported by DALL-E 2. For other providers, use `generate_image` with descriptive prompts.

### Batch Operations

Generate multiple images from a list of prompts efficiently:

```image_gen
batch_generate(
    prompts=["sunset over ocean", "mountain landscape", "city skyline"],
    provider="gemini",
    style="photo",
    output_dir="landscapes",
    view=True
)
```

Benefits:
- Process multiple prompts in one call
- Automatic filename generation
- Progress tracking
- Optional view all results

### Provider Comparison

Compare the same prompt across multiple providers:

```image_gen
compare_providers(
    prompt="futuristic city skyline at night",
    providers=["gemini", "dalle"],
    quality="hd",
    view=True
)
```

Results are saved with provider-specific filenames for easy comparison. Perfect for:
- Evaluating provider strengths
- Choosing best result for your use case
- A/B testing prompts

## Future Enhancements (Phase 4+)

- [ ] Local Stable Diffusion support
- [ ] Image editing with masks (inpainting)
- [ ] Advanced image-to-image transformations
