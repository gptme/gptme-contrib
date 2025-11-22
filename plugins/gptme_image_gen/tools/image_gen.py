"""
Image generation tool for multi-provider image generation.

Supports Google Gemini (Imagen), OpenAI DALL-E, and more.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gptme.tools.base import ToolSpec

Provider = Literal["gemini", "dalle", "dalle2"]


@dataclass
class ImageResult:
    """Result from image generation."""

    provider: str
    prompt: str
    image_path: Path
    metadata: dict


def generate_image(
    prompt: str,
    provider: Provider = "gemini",
    size: str = "1024x1024",
    quality: str = "standard",
    output_path: str | None = None,
) -> ImageResult:
    """
    Generate an image from a text prompt.

    Args:
        prompt: Text description of image to generate
        provider: Image generation provider (gemini, dalle, dalle2)
        size: Image size (provider-specific formats)
        quality: Image quality level (standard, hd)
        output_path: Where to save image (optional, defaults to generated-{timestamp}.png)

    Returns:
        ImageResult with path and metadata
    """
    if output_path is None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"generated_{timestamp}.png"

    output_path = Path(output_path).expanduser().resolve()

    if provider == "gemini":
        result = _generate_gemini(prompt, size, quality, output_path)
    elif provider == "dalle":
        result = _generate_dalle(prompt, size, quality, output_path, model="dall-e-3")
    elif provider == "dalle2":
        result = _generate_dalle(prompt, size, quality, output_path, model="dall-e-2")
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return result


def _generate_gemini(
    prompt: str,
    size: str,
    quality: str,
    output_path: Path,
) -> ImageResult:
    """Generate image using Google Gemini/Imagen."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai not installed. Install with: pip install google-generativeai"
        )

    # Configure API key
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY environment variable not set")

    genai.configure(api_key=api_key)

    # Use Imagen model
    model = genai.GenerativeModel("imagen-3-fast-generate-001")

    response = model.generate_content(prompt)

    # Save image
    if hasattr(response, "images") and response.images:
        image_data = response.images[0]
        with open(output_path, "wb") as f:
            f.write(image_data)
    else:
        raise ValueError("No image data in response")

    return ImageResult(
        provider="gemini",
        prompt=prompt,
        image_path=output_path,
        metadata={
            "model": "imagen-3-fast",
            "size": size,
            "quality": quality,
        },
    )


def _generate_dalle(
    prompt: str,
    size: str,
    quality: str,
    output_path: Path,
    model: str,
) -> ImageResult:
    """Generate image using OpenAI DALL-E."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai not installed. Install with: pip install openai")

    client = OpenAI()

    # Generate image
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
        n=1,
    )

    # Get image data (URL or base64)
    image_data = response.data[0]

    # Download and save
    if hasattr(image_data, "url") and image_data.url:
        import requests

        img_response = requests.get(image_data.url)
        with open(output_path, "wb") as f:
            f.write(img_response.content)
    elif hasattr(image_data, "b64_json") and image_data.b64_json:
        img_bytes = base64.b64decode(image_data.b64_json)
        with open(output_path, "wb") as f:
            f.write(img_bytes)
    else:
        raise ValueError("No image data in response")

    return ImageResult(
        provider="dalle" if model == "dall-e-3" else "dalle2",
        prompt=prompt,
        image_path=output_path,
        metadata={
            "model": model,
            "size": size,
            "quality": quality,
        },
    )


def _execute_generate_image(
    prompt: str,
    provider: Provider = "gemini",
    size: str = "1024x1024",
    quality: str = "standard",
    output_path: str | None = None,
) -> str:
    """Execute image generation and format results."""
    result = generate_image(prompt, provider, size, quality, output_path)

    output = [
        "=== Image Generated ===\n",
        f"Provider: {result.provider}",
        f"Prompt: {result.prompt}",
        f"Saved to: {result.image_path}",
        f"\nMetadata:",
        f"  Model: {result.metadata.get('model')}",
        f"  Size: {result.metadata.get('size')}",
        f"  Quality: {result.metadata.get('quality')}",
    ]

    return "\n".join(output)


# Tool specification
image_gen_tool = ToolSpec(
    name="image_gen",
    desc="Multi-provider image generation",
    instructions="""Use this tool to generate images from text descriptions.

Supports multiple providers:
- gemini: Google's Imagen 3 (fast, high quality)
- dalle: OpenAI DALL-E 3 (creative, detailed)
- dalle2: OpenAI DALL-E 2 (faster, cheaper)

Best for:
- Creating diagrams and visualizations
- Generating UI mockups
- Creating presentation graphics
- Prototyping visual concepts
- Illustrating documentation

Arguments:
- prompt: Text description of image (required)
- provider: Image provider (optional, default: gemini)
- size: Image size like "1024x1024" (optional, default: 1024x1024)
- quality: "standard" or "hd" (optional, default: standard)
- output_path: Where to save (optional, auto-generated if not specified)
    """,
    examples="""
### Generate architecture diagram with Gemini

> User: Create a diagram showing microservices architecture
> Assistant: I'll generate a visualization using Gemini.
```image_gen
generate_image(
    prompt=\"\"\"Technical architecture diagram showing microservices architecture
    with API gateway, service mesh, multiple backend services, and databases.
    Clean, professional style with labeled components.\"\"\",
    provider="gemini",
    output_path="diagrams/microservices.png"
)
```
> System: === Image Generated ===
> Saved to: diagrams/microservices.png

### Generate logo with DALL-E

> User: Generate a modern logo for an AI coding assistant
> Assistant: I'll create a logo using DALL-E 3.
```image_gen
generate_image(
    prompt="Modern minimalist logo for an AI coding assistant. 
    Abstract geometric shapes suggesting intelligence and code.
    Professional, tech-forward aesthetic. Flat design.",
    provider="dalle",
    quality="hd",
    output_path="branding/logo.png"
)
```

### Compare providers for concept art

> User: Generate concept art for a sci-fi interface
> Assistant: Let me generate versions with both providers.
```image_gen
generate_image(
    prompt="Futuristic holographic user interface with data visualizations
    and control panels. Cyberpunk aesthetic, glowing blue and purple.",
    provider="gemini",
    output_path="concepts/interface-gemini.png"
)
generate_image(
    prompt="Futuristic holographic user interface with data visualizations
    and control panels. Cyberpunk aesthetic, glowing blue and purple.",
    provider="dalle",
    output_path="concepts/interface-dalle.png"
)
```
    """,
    functions=[generate_image],
    block_types=["image_gen"],
)
