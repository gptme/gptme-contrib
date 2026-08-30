"""Body adapters: the Brain↔Body contract for BobBrain presence nodes.

See adapter.py for the design rule (goals, not control) and capability
gating. MavsdkAdapter (PX4 quad / SITL) lives in mavsdk_adapter.py behind
the optional ``gptme-voice[body]`` extra.
"""

from .adapter import (
    CAP_ALTITUDE,
    CAP_MOVE,
    CAP_ROTATE,
    BodyAdapter,
    NullAdapter,
    body_adapter_from_env,
    body_tool_schemas,
)

__all__ = [
    "CAP_ALTITUDE",
    "CAP_MOVE",
    "CAP_ROTATE",
    "BodyAdapter",
    "NullAdapter",
    "body_adapter_from_env",
    "body_tool_schemas",
]
