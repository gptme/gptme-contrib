"""Shared block-file registry for gptme agent arm dispatch.

The credential-survival sublayer extracted from Bob's fleet so that a second
consumer (any forked agent) can make the same dispatch decision from the same
files, without importing Bob-only modules.

Policy-free by design: this package owns the *wire contract* (filenames,
timestamp shape, fail-open reads, never-shorten writes) and the *evaluation
semantics* (a block is active while its deadline is in the future). Callers
supply the policy — which route, which scoped credential context, which backend
rules apply.

Quick start::

    from gptme_block_registry import BlockRegistry

    registry = BlockRegistry(workspace / "state" / "backend-quota")
    checks = registry.arm_checks("gptme", "glm-5.2")
    checks.append(registry.backend_check("gptme"))
    checks.append(registry.openrouter_check(context="autonomous"))
    verdict = registry.check(checks)
    if verdict.blocked:
        print(verdict.reason, verdict.until)
"""

from .contract import (
    ARM_BLOCK_KINDS,
    arm_block_path,
    backend_block_path,
    format_timestamp,
    is_canonical_timestamp,
    model_safe,
    openrouter_block_path,
    parse_until,
    pool_block_path,
)
from .openrouter import block_deadline, limit_window_from_text
from .registry import BlockCheck, BlockRegistry, BlockVerdict
from .slot_circuit_breaker import (
    AUTH_DEATH,
    NEUTRAL,
    PRODUCTIVE,
    CircuitState,
    decide_respawn,
)
from .writers import clear_block, read_block_until, write_block

__all__ = [
    "ARM_BLOCK_KINDS",
    "AUTH_DEATH",
    "BlockCheck",
    "BlockRegistry",
    "BlockVerdict",
    "CircuitState",
    "NEUTRAL",
    "PRODUCTIVE",
    "arm_block_path",
    "backend_block_path",
    "block_deadline",
    "clear_block",
    "decide_respawn",
    "format_timestamp",
    "is_canonical_timestamp",
    "limit_window_from_text",
    "model_safe",
    "openrouter_block_path",
    "parse_until",
    "pool_block_path",
    "read_block_until",
    "write_block",
]
