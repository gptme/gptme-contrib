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
    count: int = 1,
    view: bool = False,
) -> ImageResult | list[ImageResult]:
    """
    Generate an image from a text prompt.

    Args:
        prompt: Text description of image to generate
        provider: Image generation provider (gemini, dalle, dalle2)
        size: Image size (provider-specific formats)
        quality: Image quality level (standard, hd)
        output_path: Where to save image (optional, defaults to generated-{timestamp}.png)
        count: Number of images to generate (default: 1)
        view: Whether to display generated images to LLM (default: False)

    Returns:
        ImageResult with path and metadata (if count=1)
        List of ImageResults (if count>1)
    """
    # Validate count
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")

    # Generate timestamp once for all images
    if output_path is None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Generate multiple images if count > 1
    results = []
    for i in range(count):
        # Determine output path for this iteration
        if output_path is None:
            if count > 1:
                current_output = f"generated_{timestamp}_{i + 1:03d}.png"
            else:
                current_output = f"generated_{timestamp}.png"
        else:
            # Add number suffix for multiple images
            if count > 1:
                path = Path(output_path)
                stem = path.stem
                suffix = path.suffix
                current_output = str(path.parent / f"{stem}_{i + 1:03d}{suffix}")
            else:
                current_output = output_path

        current_path = Path(current_output).expanduser().resolve()
        current_path.parent.mkdir(parents=True, exist_ok=True)

        # Generate single image
        try:
            if provider == "gemini":
                result = _generate_gemini(prompt, size, quality, current_path)
            elif provider == "dalle":
                result = _generate_dalle(
                    prompt, size, quality, current_path, model="dall-e-3"
                )
            elif provider == "dalle2":
                result = _generate_dalle(
                    prompt, size, quality, current_path, model="dall-e-2"
                )
            else:
                raise ValueError(f"Unknown provider: {provider}")

            results.append(result)

        except Exception as e:
            # Add error context
            raise RuntimeError(
                f"Failed to generate image {i + 1}/{count} with {provider}: {e}"
            ) from e

    # View images if requested
    if view:
        try:
            from gptme.tools.vision import view_image

            for result in results:
                view_image(result.image_path)
        except ImportError:
            # Vision tool not available, skip viewing
            pass

    # Return single result or list based on count
    if count == 1:
        return results[0]
    else:
        return results


def _generate_gemini(
    prompt: str,
    size: str,
    quality: str,
    output_path: Path,
) -> ImageResult:
    """Generate image using Google Gemini/Imagen."""
    try:
        import google.generativeai as genai  # type: ignore[import-not-found]
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
    response = client.images.generate(  # type: ignore[call-overload]
        prompt=prompt,
        model=model,
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
    count: int = 1,
    view: bool = False,
) -> str:
    """Execute image generation and format results."""
    result = generate_image(prompt, provider, size, quality, output_path, count, view)

    # Handle single or multiple results
    results_list = [result] if isinstance(result, ImageResult) else result

    output = []
    if len(results_list) > 1:
        output.append(f"=== {len(results_list)} Images Generated ===\n")
    else:
        output.append("=== Image Generated ===\n")

    for i, img_result in enumerate(results_list, 1):
        if len(results_list) > 1:
            output.append(f"\n--- Image {i}/{len(results_list)} ---")

        output.extend(
            [
                f"Provider: {img_result.provider}",
                f"Prompt: {img_result.prompt}",
                f"Saved to: {img_result.image_path}",
                "\nMetadata:",
                f"  Model: {img_result.metadata.get('model')}",
                f"  Size: {img_result.metadata.get('size')}",
                f"  Quality: {img_result.metadata.get('quality')}",
            ]
        )

    if view:
        output.append("\n✓ Images displayed to assistant for review")

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
- count: Number of images to generate (optional, default: 1)
- view: Display generated images to assistant (optional, default: False)

New in Phase 1:
- Multiple options: Use count=3 to generate 3 variations for comparison
- View integration: Use view=True to display images to assistant for verification/feedback
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

### Generate multiple options and view them

> User: Create a logo and show me the options
> Assistant: I'll generate 3 logo variations and display them for review.
```image_gen
generate_image(
    prompt="Modern minimalist logo for tech startup. Geometric, professional, memorable.",
    provider="gemini",
    count=3,
    view=True,
    output_path="logos/option.png"
)
```
> System: === 3 Images Generated ===
>
> --- Image 1/3 ---
> Saved to: logos/option_001.png
>
> --- Image 2/3 ---
> Saved to: logos/option_002.png
>
> --- Image 3/3 ---
> Saved to: logos/option_003.png
>
> ✓ Images displayed to assistant for review
> Assistant: I can see all three options. Option 2 has the best balance of simplicity and recognition.

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
