"""Run loop framework for autonomous AI agent operation."""

from typing import Any

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.base import BaseRunLoop
from gptme_runloops.email import EmailRun
from gptme_runloops.project_monitoring import ProjectMonitoringRun
from gptme_runloops.team import TeamRun
from gptme_runloops.utils.executor import (
    ClaudeCodeExecutor,
    Executor,
    GptmeExecutor,
    get_executor,
    list_backends,
)

# pm_dispatch symbols are re-exported lazily (PEP 562) instead of eagerly. An
# eager `from gptme_runloops.pm_dispatch import ...` here imports the submodule
# during the parent package import, so running `python -m
# gptme_runloops.pm_dispatch` finds it already in sys.modules before runpy
# executes it as __main__ — emitting a RuntimeWarning on every invocation
# (the project-monitoring loop runs `-m gptme_runloops.pm_dispatch` ~6x/run).
# Lazy access keeps `from gptme_runloops import DispatchLedger` working while
# avoiding the import at package-load time.
_PM_DISPATCH_EXPORTS = frozenset(
    {
        "DispatchLedger",
        "LaneDispatcher",
        "LedgerEntry",
        "SlotItem",
        "SlotManager",
        "classify_lane",
        "derive_slot_key",
        "partition_items",
    }
)


def __getattr__(name: str) -> Any:
    if name in _PM_DISPATCH_EXPORTS:
        from gptme_runloops import pm_dispatch

        return getattr(pm_dispatch, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "BaseRunLoop",
    "AutonomousRun",
    "EmailRun",
    "ProjectMonitoringRun",
    "TeamRun",
    "Executor",
    "GptmeExecutor",
    "ClaudeCodeExecutor",
    "get_executor",
    "list_backends",
    # pm_dispatch
    "DispatchLedger",
    "LaneDispatcher",
    "LedgerEntry",
    "SlotItem",
    "SlotManager",
    "classify_lane",
    "derive_slot_key",
    "partition_items",
]
