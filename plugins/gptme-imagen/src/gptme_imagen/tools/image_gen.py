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

from .cost_tracker import get_cost_tracker


def _get_api_key(env_var: str) -> str | None:
    """Get API key from gptme config first, then fall back to os.environ."""
    try:
        from gptme.config import get_config

        return get_config().get_env(env_var)
    except ImportError:
        return os.environ.get(env_var)


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

# Map provider names to their environment variable names
PROVIDER_ENV_VAR = {
    "gemini": "GOOGLE_API_KEY",
    "dalle": "OPENAI_API_KEY",
    "dalle2": "OPENAI_API_KEY",
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
    images: list[str] | str | None = None,
) -> ImageResult | list[ImageResult]:
    """
    Generate an image from a text prompt, optionally using reference images.

    Args:
        prompt: Text description of image to generate or modifications to apply
        provider: Image generation provider (gemini, dalle, dalle2)
        size: Image size (provider-specific formats)
        quality: Image quality level (standard, hd)
        output_path: Where to save image (optional, defaults to generated-{timestamp}.png)
        count: Number of images to generate (default: 1)
        view: Whether to display generated images to LLM (default: False)
        style: Apply style preset to enhance prompt (optional)
        enhance: Use LLM to enhance prompt for better results (optional, default: False)
        show_progress: Show progress indicators during generation (default: True)
        images: Input image(s) for multimodal generation (optional). Can be:
            - A single image path (str) for modification/editing
            - A list of image paths for multi-reference generation
            Examples: character references, style references, images to modify,
            scene elements to combine. Currently only supported by Gemini.

    Returns:
        ImageResult with path and metadata (if count=1)
        List of ImageResults (if count>1)

    Examples:
        # Generate from text only
        result = generate_image("a sunset over mountains")

        # Modify a single image
        result = generate_image(
            prompt="change the background to a beach",
            images="portrait.png"
        )

        # Multi-reference generation (character + style)
        result = generate_image(
            prompt="create a portrait in this style with this character",
            images=["character_ref.png", "style_ref.png"]
        )
    """
    # Validate count
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")

    # Validate provider early
    valid_providers = ("gemini", "dalle", "dalle2")
    if provider not in valid_providers:
        raise ValueError(
            f"Unknown provider: {provider}. Must be one of: {valid_providers}"
        )

    # Normalize and validate images parameter
    image_paths: list[Path] | None = None
    if images is not None:
        # Normalize to list
        if isinstance(images, str):
            images = [images]

        # Only Gemini supports multimodal generation with images
        if provider != "gemini":
            raise ValueError(
                f"Image references only supported for gemini provider, got {provider}. "
                "For DALL-E, use generate_variation() for variations without text guidance."
            )

        # Validate all images exist
        image_paths = []
        for img_path in images:
            path = Path(img_path).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"Image not found: {img_path}")
            image_paths.append(path)

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
                result = _generate_gemini(
                    prompt, size, quality, current_path, image_paths
                )
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
            if "api key" in str(e).lower():
                env_var = PROVIDER_ENV_VAR.get(provider, f"{provider.upper()}_API_KEY")
                error_msg += f": Missing or invalid API key. Check your {env_var} environment variable."
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
    images: list[Path] | None = None,
) -> ImageResult:
    """Generate image using Google Gemini/Imagen.

    Args:
        prompt: Text prompt for generation or modification
        size: Image size (not currently used by Gemini API)
        quality: Image quality level
        output_path: Where to save the generated image
        images: Optional validated input image paths for multimodal generation.
            Already validated by generate_image() caller.
    """
    try:
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "google-genai not installed. Install with: pip install google-genai"
        )

    # Configure API key
    api_key = _get_api_key("GOOGLE_API_KEY") or _get_api_key("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY or GEMINI_API_KEY not set in env or config")

    # Create client with API key
    client = genai.Client(api_key=api_key)

    # Use gemini-3-pro-image-preview for multimodal image generation
    # This model can generate both text and images
    model_name = "gemini-3-pro-image-preview"

    # Build contents list with prompt and optional images
    contents: list = [prompt]

    if images:
        try:
            from PIL import Image
        except ImportError:
            raise ImportError("PIL not installed. Install with: pip install Pillow")

        # Load and add each validated image (paths already validated by caller)
        for img_path in images:
            contents.append(Image.open(img_path))

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    # Save image - handle new SDK response format
    image_saved = False
    for part in response.parts:
        # Use the as_image() helper method from the new SDK
        img = part.as_image()
        if img is not None:
            img.save(str(output_path))
            image_saved = True
            break

    if not image_saved:
        raise ValueError("No image data found in response")

    # Calculate and record cost
    cost_tracker = get_cost_tracker()
    cost = cost_tracker.calculate_cost(
        provider="gemini", quality=quality, count=1, model=model_name
    )
    cost_tracker.record_generation(
        provider="gemini",
        prompt=prompt,
        cost=cost,
        model=model_name,
        size=size,
        quality=quality,
        count=1,
        output_path=str(output_path),
    )

    return ImageResult(
        provider="gemini",
        prompt=prompt,
        image_path=output_path,
        metadata={
            "model": model_name,
            "size": size,
            "quality": quality,
            "cost_usd": cost,
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

    api_key = _get_api_key("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set in env or config")
    client = OpenAI(api_key=api_key)

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

    # Calculate and record cost
    cost_tracker = get_cost_tracker()
    provider = "dalle" if model == "dall-e-3" else "dalle2"
    cost = cost_tracker.calculate_cost(
        provider=provider, quality=quality, count=1, model=model
    )
    cost_tracker.record_generation(
        provider=provider,
        prompt=prompt,
        cost=cost,
        model=model,
        size=size,
        quality=quality,
        count=1,
        output_path=str(output_path),
    )

    return ImageResult(
        provider=provider,
        prompt=prompt,
        image_path=output_path,
        metadata={
            "model": model,
            "size": size,
            "quality": quality,
            "cost_usd": cost,
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
    images: list[str] | str | None = None,
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
        images,
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


# Cost tracking helper functions
def generate_variation(
    image_path: str,
    provider: Provider = "dalle2",
    count: int = 1,
    size: str = "1024x1024",
    output_path: str | None = None,
    view: bool = False,
) -> ImageResult | list[ImageResult]:
    """
    Generate variations of an existing image.

    Note: Currently only DALL-E 2 supports image variations.
    For other providers, use generate_image with a descriptive prompt.

    Args:
        image_path: Path to the input image
        provider: Image generation provider (currently only "dalle2" supported)
        count: Number of variations to generate (default: 1)
        size: Output image size (default: "1024x1024")
        output_path: Where to save variations (optional)
        view: Whether to display generated images to LLM (default: False)

    Returns:
        ImageResult (if count=1) or list of ImageResults (if count>1)

    Example:
        variation = generate_variation(
            image_path="original.png",
            provider="dalle2",
            count=3,
            view=True
        )
    """
    if provider != "dalle2":
        raise ValueError(
            f"Image variations only supported for dalle2, got {provider}. "
            "For other providers, use generate_image with a descriptive prompt."
        )

    # Validate image exists
    image_file = Path(image_path)
    if not image_file.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Load and encode image
    with open(image_file, "rb") as f:
        image_data = f.read()

    # Generate variations using OpenAI API
    import openai

    api_key = _get_api_key("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set in env or config")
    client = openai.OpenAI(api_key=api_key)

    # Generate variations
    results = []
    for i in range(count):
        if count > 1:
            print(f"  → Variation {i + 1}/{count}...")

        from typing import cast

        response = client.images.create_variation(
            image=image_data,
            n=1,
            size=cast(Literal["256x256", "512x512", "1024x1024"], size),
        )

        # Download and save image
        if not response.data:
            raise ValueError("No image data in response")
        img_url = response.data[0].url
        img_data = __import__("requests").get(img_url).content

        # Determine output path
        if output_path:
            if count > 1:
                base, ext = os.path.splitext(output_path)
                save_path = f"{base}_{i + 1:03d}{ext}"
            else:
                save_path = output_path
        else:
            timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = f"variation_{timestamp}_{i + 1:03d}.png"

        # Save image
        save_file = Path(save_path)
        save_file.parent.mkdir(parents=True, exist_ok=True)
        save_file.write_bytes(img_data)

        # Track cost
        cost = 0.02  # DALL-E 2 variation cost per image
        tracker = get_cost_tracker()
        tracker.record_generation(
            provider="dalle2",
            prompt=f"variation of {image_path}",
            cost=cost,
            model="dall-e-2",
            size=size,
        )

        result = ImageResult(
            provider="dalle2",
            prompt=f"variation of {image_path}",
            image_path=save_file,
            metadata={"model": "dall-e-2", "size": size, "type": "variation"},
        )
        results.append(result)

    # Handle view parameter
    if view:
        from gptme.tools.vision import view_image

        for result in results:
            view_image(result.image_path)

    return results[0] if count == 1 else results


def batch_generate(
    prompts: list[str],
    provider: Provider = "gemini",
    output_dir: str | None = None,
    view: bool = False,
    **kwargs,
) -> list[ImageResult]:
    """
    Generate multiple images from a list of prompts.

    Args:
        prompts: List of text descriptions for images to generate
        provider: Image generation provider (gemini, dalle, dalle2)
        output_dir: Directory to save images (optional, defaults to current dir)
        view: Whether to display all generated images to LLM (default: False)
        **kwargs: Additional arguments passed to generate_image (size, quality, style, etc.)

    Returns:
        List of ImageResults, one per prompt

    Example:
        results = batch_generate(
            prompts=["sunset over ocean", "mountain landscape", "city skyline"],
            provider="gemini",
            style="photo",
            output_dir="batch_images"
        )
    """
    results = []
    total = len(prompts)

    for i, prompt in enumerate(prompts, 1):
        # Generate output path if output_dir specified
        output_path = None
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            # Create filename from sanitized prompt
            safe_name = "".join(c if c.isalnum() else "_" for c in prompt[:30])
            output_path = f"{output_dir}/{safe_name}_{i:03d}.png"

        print(f"🎨 Batch generation: {i}/{total}")

        # Ensure count=1 for batch generation (one image per prompt)
        kwargs_copy = kwargs.copy()
        kwargs_copy.pop("count", None)  # Remove count if present

        result = generate_image(
            prompt=prompt,
            provider=provider,
            output_path=output_path,
            view=False,  # Don't view individual images
            count=1,  # Explicitly set count=1
            **kwargs_copy,
        )

        # generate_image returns ImageResult when count=1
        assert isinstance(result, ImageResult), "Expected single ImageResult"
        results.append(result)

    if view:
        # Display all images to LLM after generation
        from gptme.tools.vision import view_image

        print(f"\n✓ Displaying {total} generated images to assistant")
        for result in results:
            view_image(result.image_path)

    return results


def compare_providers(
    prompt: str,
    providers: list[Provider] | None = None,
    view: bool = True,
    **kwargs,
) -> dict[str, ImageResult]:
    """
    Generate the same prompt across multiple providers for comparison.

    Args:
        prompt: Text description of image to generate
        providers: List of providers to compare (default: all available)
        view: Whether to display all generated images to LLM (default: True)
        **kwargs: Additional arguments passed to generate_image (size, quality, etc.)

    Returns:
        Dictionary mapping provider name to ImageResult

    Example:
        results = compare_providers(
            prompt="futuristic city skyline at night",
            providers=["gemini", "dalle"],
            quality="hd"
        )
    """
    if providers is None:
        providers = ["gemini", "dalle"]

    results: dict[str, ImageResult] = {}
    total = len(providers)

    for i, provider in enumerate(providers, 1):
        print(f"🔍 Comparing providers: {i}/{total} ({provider})")

        # Generate with unique output path per provider
        base_name = "".join(c if c.isalnum() else "_" for c in prompt[:30])
        output_path = f"comparison/{base_name}_{provider}.png"

        try:
            # Ensure count=1 for comparison (one image per provider)
            kwargs_copy = kwargs.copy()
            kwargs_copy.pop("count", None)  # Remove count if present

            result = generate_image(
                prompt=prompt,
                provider=provider,
                output_path=output_path,
                view=False,  # Don't view individual images
                count=1,  # Explicitly set count=1
                **kwargs_copy,
            )

            # generate_image returns ImageResult when count=1
            assert isinstance(result, ImageResult), "Expected single ImageResult"
            results[provider] = result
        except Exception as e:
            print(f"❌ {provider} failed: {e}")
            continue

    if view and results:
        # Display all images to LLM for comparison
        from gptme.tools.vision import view_image

        print(f"\n✓ Displaying {len(results)} images for comparison")
        for prov, result in results.items():
            print(f"\n--- {prov.upper()} ---")
            view_image(result.image_path)

    return results


def get_total_cost(
    provider: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> float:
    """
    Get total cost of image generations.

    Args:
        provider: Filter by provider (gemini, dalle, dalle2)
        start_date: Start date in ISO format (e.g., "2024-01-01")
        end_date: End date in ISO format

    Returns:
        Total cost in USD

    Example:
        >>> cost = get_total_cost()  # All time
        >>> print(f"Total spent: ${cost:.2f}")
        >>>
        >>> cost = get_total_cost(provider="gemini")  # Gemini only
        >>> cost = get_total_cost(start_date="2024-11-01")  # This month
    """
    tracker = get_cost_tracker()
    return tracker.get_total_cost(provider, start_date, end_date)


def get_cost_breakdown(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, float]:
    """
    Get cost breakdown by provider.

    Args:
        start_date: Start date in ISO format
        end_date: End date in ISO format

    Returns:
        Dictionary mapping provider to total cost

    Example:
        >>> breakdown = get_cost_breakdown()
        >>> for provider, cost in breakdown.items():
        ...     print(f"{provider}: ${cost:.2f}")
    """
    tracker = get_cost_tracker()
    return tracker.get_cost_breakdown(start_date, end_date)


def get_generation_history(
    limit: int = 50,
    provider: str | None = None,
) -> list[dict]:
    """
    Get recent generation history.

    Args:
        limit: Maximum number of records (default: 50)
        provider: Filter by provider

    Returns:
        List of generation records with timestamps, prompts, costs, etc.

    Example:
        >>> history = get_generation_history(limit=10)
        >>> for gen in history:
        ...     print(f"{gen['timestamp']}: {gen['prompt'][:50]}... (${gen['cost_usd']:.3f})")
    """
    tracker = get_cost_tracker()
    return tracker.get_generation_history(limit, provider)


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
- images: Input image(s) for multimodal generation (optional, Gemini only). Can be:
    - A single image path (str) for modification/editing
    - A list of image paths for multi-reference generation
    Examples: character references, style references, images to modify, scene elements to combine.

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

### Batch generation (Phase 3.2)

> User: Generate images for these three concepts
> Assistant: I'll use batch generation for efficiency.
```image_gen
batch_generate(
    prompts=["sunset over ocean", "mountain landscape", "city skyline"],
    provider="gemini",
    style="photo",
    output_dir="landscapes",
    view=True
)
```
> System: 🎨 Batch generation: 1/3
> 🎨 Batch generation: 2/3
> 🎨 Batch generation: 3/3
> ✓ Displaying 3 generated images to assistant

### Provider comparison (Phase 3.2)

> User: Compare how different providers render this prompt
> Assistant: I'll generate with multiple providers for comparison.
```image_gen
compare_providers(
    prompt="futuristic holographic interface with data visualizations",
    providers=["gemini", "dalle"],
    quality="hd"
)
```
> System: 🔍 Comparing providers: 1/2 (gemini)
> 🔍 Comparing providers: 2/2 (dalle)
> ✓ Displaying 2 images for comparison

### Image variations (Phase 3.2)

> User: Create variations of this logo
> Assistant: I'll generate variations using DALL-E 2.
```image_gen
generate_variation(
    image_path="logo_original.png",
    provider="dalle2",
    count=4,
    view=True
)
```
> System: → Variation 1/4... ✓
> → Variation 2/4... ✓
> → Variation 3/4... ✓
> → Variation 4/4... ✓
> ✓ Images displayed to assistant for review

### Image modification (image + text)

> User: Change the background of my avatar to a sunset beach
> Assistant: I'll modify the image using Gemini's multimodal image generation.
```image_gen
generate_image(
    prompt="change the background to a beautiful sunset beach scene with palm trees",
    images="avatar.png",
    view=True
)
```
> System: 🎨 Generating with gemini (using 1 reference image)...
> ✅ Image saved: generated_20241224_080500.png
> ✓ Image displayed to assistant for review

> User: Add glasses and a hat to this portrait
> Assistant: I'll modify the portrait to add accessories.
```image_gen
generate_image(
    prompt="add round glasses and a blue baseball cap",
    images="portrait.png",
    output_path="portrait_with_accessories.png"
)
```
> System: 🎨 Generating with gemini (using 1 reference image)...
> ✅ Image saved: portrait_with_accessories.png
    """,
    functions=[
        generate_image,
        generate_variation,
        batch_generate,
        compare_providers,
    ],
    block_types=["image_gen"],
)
