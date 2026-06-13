"""Regression tests for the package's public import surface.

Covers the PEP 562 lazy re-export of ``pm_dispatch`` symbols and the runpy
RuntimeWarning that the previous eager re-export emitted on every
``python -m gptme_runloops.pm_dispatch`` invocation.
"""

import importlib
import subprocess
import sys

import gptme_runloops


def test_all_names_are_importable():
    """Every name in __all__ resolves (eager imports + lazy pm_dispatch re-exports)."""
    for name in gptme_runloops.__all__:
        assert hasattr(gptme_runloops, name), f"{name} not accessible on package"


def test_lazy_pm_dispatch_reexports():
    """pm_dispatch symbols are re-exported and identical to the submodule's."""
    pm = importlib.import_module("gptme_runloops.pm_dispatch")
    for name in (
        "DispatchLedger",
        "LaneDispatcher",
        "derive_slot_key",
        "partition_items",
    ):
        assert getattr(gptme_runloops, name) is getattr(pm, name)


def test_unknown_attribute_raises_attributeerror():
    try:
        gptme_runloops.does_not_exist  # noqa: B018
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError for unknown attribute")


def test_run_as_module_emits_no_runpy_warning():
    """`python -m gptme_runloops.pm_dispatch` must not emit the runpy RuntimeWarning.

    The eager re-export pre-registered pm_dispatch in sys.modules during the
    parent package import, so runpy warned "found in sys.modules ... prior to
    execution" before running it as __main__.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "gptme_runloops.pm_dispatch",
        ],
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode in (0, 1), proc.stderr
    assert "RuntimeWarning" not in combined, combined
    assert "found in sys.modules" not in combined, combined
