"""Hook exports for the headroom compressor plugin."""

from .compressor import (
    DEFAULT_MIN_COMPRESS_CHARS,
    ENABLED_ENV_VAR,
    generation_pre_hook,
    register,
)

__all__ = [
    "DEFAULT_MIN_COMPRESS_CHARS",
    "ENABLED_ENV_VAR",
    "generation_pre_hook",
    "register",
]
