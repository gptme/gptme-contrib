"""Hook exports for the tool-output trimmer plugin."""

from .summarizer import (
    SUMMARIZATION_PROMPT,
    SummarizerConfig,
    apply_summarization,
    get_summarizer_config,
)
from .summarizer import (
    generation_pre_hook as summarization_generation_pre_hook,
)
from .summarizer import (
    register as register_summarizer,
)
from .trimmer import (
    BYPASS_ENV_VAR,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_PRESSURE_CHARS,
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_RECENT_TURNS,
    TOOL_OUTPUT_PREFIXES,
    TRIMMED_MARKER,
    TriggerDecision,
    TrimmerConfig,
    TrimSummary,
    apply_tool_output_trimmer,
    build_trimmed_content,
    determine_trigger,
    estimate_billed_chars,
    generation_pre_hook,
    get_trimmer_config,
    reset_state,
)
from .trimmer import (
    register as register_trimmer,
)

__all__ = [
    "BYPASS_ENV_VAR",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_PRESSURE_CHARS",
    "DEFAULT_RECENT_TURNS",
    "SUMMARIZATION_PROMPT",
    "SummarizerConfig",
    "TOOL_OUTPUT_PREFIXES",
    "TRIMMED_MARKER",
    "TrimSummary",
    "TriggerDecision",
    "TrimmerConfig",
    "apply_summarization",
    "apply_tool_output_trimmer",
    "build_trimmed_content",
    "determine_trigger",
    "estimate_billed_chars",
    "generation_pre_hook",
    "get_summarizer_config",
    "get_trimmer_config",
    "register_summarizer",
    "register_trimmer",
    "reset_state",
    "summarization_generation_pre_hook",
]
