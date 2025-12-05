# gptme Plugins

Collection of plugins for [gptme](https://github.com/ErikBjare/gptme).

## Available Plugins

### 🤝 consortium
Multi-model consensus decision-making system that orchestrates multiple LLMs to provide diverse perspectives and synthesize consensus responses.

**Use for**: Important decisions, architectural choices, code review from multiple perspectives, model comparison.

[Documentation](./consortium/README.md)

### 🎨 imagen
Multi-provider image generation supporting Google Gemini (Imagen), OpenAI DALL-E, and more with a unified interface.

**Use for**: Creating diagrams, UI mockups, presentation graphics, visual prototyping.

[Documentation](./imagen/README.md)

## Installation

### 1. Configure gptme

Add to your `gptme.toml`:

```toml
[plugins]
paths = [
    "~/.config/gptme/plugins",
    # Or path to this directory
    "/path/to/gptme-contrib/plugins"
]

# Optional: enable specific plugins only
enabled = ["consortium", "imagen"]
```

### 2. Install Dependencies

Each plugin may have its own dependencies. Install as needed:

```bash
# For consortium (uses gptme's existing model infrastructure)
# No additional dependencies needed

# For image_gen
pip install google-generativeai openai requests
```

### 3. Set Up API Keys

```bash
# For Gemini (image_gen)
export GOOGLE_API_KEY="your-key"

# For DALL-E (image_gen)
export OPENAI_API_KEY="your-key"
```

## Usage

Once configured, plugins are automatically loaded and their tools become available:

```bash
gptme
```
