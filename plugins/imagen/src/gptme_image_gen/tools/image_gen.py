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
Style = Literal[
    "photo",
    "illustration",
    "sketch",
    "technical-diagram",
    "flat-design",
    "cyberpunk",
    "watercolor",
    "oil-painting",
]

# Style preset descriptions
STYLE_PRESETS = {
    "photo": "Photorealistic style with natural lighting, high detail, camera-like quality",
    "illustration": "Artistic illustration style, stylized and creative interpretation",
    "sketch": "Hand-drawn sketch style, pencil or pen lines, conceptual and loose",
    "technical-diagram": "Clean technical diagram style with labels, professional and informative",
    "flat-design": "Minimalist flat design with geometric shapes and solid colors",
    "cyberpunk": "Futuristic cyberpunk aesthetic with neon lights, dark tones, technology theme",
    "watercolor": "Soft watercolor painting style with flowing colors and artistic texture",
    "oil-painting": "Classical oil painting style with rich colors and textured brushstrokes",
}


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
    style: Style | None = None,
    enhance: bool = False,
    show_progress: bool = True,
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
        style: Apply style preset to enhance prompt (optional)
        enhance: Use LLM to enhance prompt for better results (optional, default: False)
        show_progress: Show progress indicators during generation (default: True)

    Returns:
        ImageResult with path and metadata (if count=1)
        List of ImageResults (if count>1)
    """
    # Validate count
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")

    # Enhance prompt BEFORE applying style preset
    # This ensures enhancement works on user's original prompt
    if enhance:
        prompt = _enhance_prompt(prompt)

    # Apply style preset if specified (after enhancement)
    if style:
        style_desc = STYLE_PRESETS.get(style, "")
        # Strip trailing period to avoid awkward punctuation
        prompt = f"{prompt.rstrip('.')}. Style: {style_desc}"

    # Generate timestamp once for all images
    if output_path is None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Show initial progress message
    if show_progress:
        if count > 1:
            print(f"🎨 Generating {count} images with {provider}...")
        else:
            print(f"🎨 Generating image with {provider}...")

    # Generate multiple images if count > 1
    results = []
    for i in range(count):
        # Show progress for current image
        if show_progress and count > 1:
            print(f"  → Image {i + 1}/{count}...", end=" ", flush=True)
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

            # Show completion for this image
            if show_progress and count > 1:
                print("✓")

        except Exception as e:
            # Show error indicator
            if show_progress and count > 1:
                print("✗")

            # Add detailed error context
            error_msg = f"Failed to generate image {i + 1}/{count} with {provider}"
            if "API key" in str(e).lower():
                error_msg += f": Missing or invalid API key. Check your {provider.upper()}_API_KEY environment variable."
            elif "quota" in str(e).lower() or "rate limit" in str(e).lower():
                error_msg += (
                    ": API quota or rate limit exceeded. Wait a moment and try again."
                )
            elif "network" in str(e).lower() or "connection" in str(e).lower():
                error_msg += (
                    ": Network connection issue. Check your internet connection."
                )
            else:
                error_msg += f": {e}"

            raise RuntimeError(error_msg) from e

    # Show final completion message
    if show_progress:
        if count > 1:
            print(f"✅ Generated {len(results)}/{count} images successfully")
        else:
            print("✅ Image generated successfully")

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


def _enhance_prompt(prompt: str) -> str:
    """
    Enhance a prompt with quality keywords and style improvements.

    This uses template-based enhancement. Future versions could use LLM.
    """
    # Quality keywords to add if not present
    quality_keywords = [
        "high quality",
        "detailed",
        "professional",
        "4k",
        "hd",
        "masterpiece",
    ]

    # Check if prompt already has quality keywords
    has_quality = any(kw in prompt.lower() for kw in quality_keywords)

    # Build enhanced prompt
    enhanced_parts = [prompt]

    # Add quality descriptor if missing
    if not has_quality:
        enhanced_parts.append("high quality, detailed, professional")

    # Add composition guidance
    if len(prompt.split()) < 10:  # Short prompt needs more detail
        enhanced_parts.append("well-composed, clear focus")

    # Join with proper formatting
    enhanced = ", ".join(enhanced_parts)

    return enhanced


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
    style: Style | None = None,
    enhance: bool = False,
    show_progress: bool = True,
) -> str:
    """Execute image generation and format results."""
    result = generate_image(
        prompt,
        provider,
        size,
        quality,
        output_path,
        count,
        view,
        style,
        enhance,
        show_progress,
    )

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
- style: Apply style preset (optional, choices: photo, illustration, sketch, technical-diagram, flat-design, cyberpunk, watercolor, oil-painting)
- enhance: Auto-enhance prompt for better results (optional, default: False)
- show_progress: Show progress indicators during generation (optional, default: True)

Phase 1 Features:
- Multiple options: Use count=3 to generate 3 variations for comparison
- View integration: Use view=True to display images to assistant for verification/feedback

Phase 2 Features:
- Style presets: Use style="technical-diagram" for common styles
- Prompt enhancement: Use enhance=True to auto-improve prompts
- Progress indicators: Automatic progress tracking for multi-image generation
- Enhanced error messages: Clear, actionable error messages with recovery suggestions
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

### Use style presets (Phase 2)

> User: Create a technical architecture diagram
> Assistant: I'll use the technical-diagram style preset for clarity.
```image_gen
generate_image(
    prompt="Microservices architecture with API gateway and databases",
    style="technical-diagram",
    provider="gemini",
    output_path="diagrams/architecture.png"
)
```

### Enhance prompts automatically (Phase 2)

> User: Generate a logo
> Assistant: I'll enhance the prompt for better results.
```image_gen
generate_image(
    prompt="logo for tech startup",
    enhance=True,
    provider="dalle",
    quality="hd",
    output_path="branding/logo.png"
)
```
> System: === Image Generated ===
> Enhanced prompt: "logo for tech startup, high quality, detailed, professional, well-composed, clear focus"

### Progress indicators for multi-image generation

> User: Generate 5 logo variations
> Assistant: I'll generate multiple variations with progress tracking.
```image_gen
generate_image(
    prompt="tech startup logo",
    provider="gemini",
    count=5,
    output_path="logos/variation.png"
)
```
> System: 🎨 Generating 5 images with gemini...
>   → Image 1/5... ✓
>   → Image 2/5... ✓
>   → Image 3/5... ✓
>   → Image 4/5... ✓
>   → Image 5/5... ✓
> ✅ Generated 5/5 images successfully

### Combine Phase 2 features

> User: Create watercolor illustrations of office scenes
> Assistant: I'll generate multiple watercolor-style illustrations.
```image_gen
generate_image(
    prompt="modern office workspace with natural light",
    style="watercolor",
    enhance=True,
    count=3,
    view=True,
    output_path="illustrations/office.png"
)
```
    """,
    functions=[generate_image],
    block_types=["image_gen"],
)
