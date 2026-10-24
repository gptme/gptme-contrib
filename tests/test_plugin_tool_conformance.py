"""Plugin tool conformance guard (gptme/gptme-contrib#1968).

Two defect classes caught here:

1. **Executor signature**: a ToolSpec whose ``execute`` callable has a required
   positional parameter beyond ``(content, args, kwargs)`` will raise at call
   time in every format — including Markdown — because gptme's dispatcher never
   passes a 4th argument.

2. **Declared-parameter reachability**: a ToolSpec that declares ``parameters``
   but whose executor never reads from ``kwargs`` silently drops every native
   (tool-format) call, which delivers input exclusively through ``kwargs``.

Both classes were found by hand in gptme/gptme-contrib#1967 (``lsp`` and
``youtube``). This test gates against their reintroduction.

Specs that set only ``functions=[...]`` (no custom ``execute``) are dispatched
through gptme's ``from_function`` path, which already handles ``kwargs``
mapping, so they are exempt from check (2).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

_PLUGINS_DIR = Path(__file__).parent.parent / "plugins"


def _collect_tool_specs() -> list[tuple[str, Any]]:
    """Return ``[(label, toolspec), ...]`` for every ToolSpec in plugins/."""
    try:
        from gptme.tools.base import ToolSpec
    except ImportError:
        pytest.skip("gptme not installed — skipping plugin conformance guard")

    results: list[tuple[str, Any]] = []
    for py_file in sorted(_PLUGINS_DIR.rglob("*.py")):
        if "test" in py_file.name or "__pycache__" in str(py_file):
            continue
        source = py_file.read_text(errors="replace")
        if "ToolSpec" not in source:
            continue
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                f"_plugin_{py_file.stem}", py_file
            )
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception:
            continue
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if isinstance(obj, ToolSpec):
                label = f"{py_file.relative_to(_PLUGINS_DIR)} :: {attr_name}"
                results.append((label, obj))
    return results


# ---------------------------------------------------------------------------
# Check 1 — executor signature
# ---------------------------------------------------------------------------


def _required_extra_params(execute_fn: object) -> list[str]:
    """Return required positional params beyond the standard three.

    gptme's dispatcher calls execute(code, args, kwargs) — exactly 3 positional
    args. Any additional *required* parameter (no default) will raise TypeError
    at call time. We check by position: the first three positional params are
    the dispatcher-supplied ones regardless of their name.
    """
    sig = inspect.signature(execute_fn)  # type: ignore[arg-type]
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    positional = [p for p in sig.parameters.values() if p.kind in positional_kinds]
    # The first three positional params are the dispatcher-supplied ones.
    extra_required = [
        p.name for p in positional[3:] if p.default is inspect.Parameter.empty
    ]
    return extra_required


@pytest.mark.parametrize(
    "label,toolspec",
    [
        pytest.param(label, ts, id=label)
        for label, ts in _collect_tool_specs()
        if ts.execute is not None
    ],
)
def test_executor_accepts_dispatcher_shape(label: str, toolspec: Any) -> None:
    """Executor must be callable with (content, args, kwargs) — no extra required args."""
    from gptme.tools.base import ToolSpec

    assert isinstance(toolspec, ToolSpec)
    assert toolspec.execute is not None
    extra = _required_extra_params(toolspec.execute)
    assert not extra, (
        f"{label}: executor has required positional parameters beyond "
        f"(content, args, kwargs): {extra!r}. "
        "gptme's dispatcher never passes these — every call will raise TypeError."
    )


# ---------------------------------------------------------------------------
# Check 2 — declared-parameter reachability via kwargs
# ---------------------------------------------------------------------------


def _reads_kwargs(source: str) -> bool:
    """Return True if the source text reads from a ``kwargs`` parameter."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        # kwargs["key"], kwargs.get("key"), kwargs.get("key", default)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "kwargs":
                return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "kwargs"
                ):
                    return True
    return False


@pytest.mark.parametrize(
    "label,toolspec",
    [
        pytest.param(label, ts, id=label)
        for label, ts in _collect_tool_specs()
        if ts.execute is not None and ts.parameters
    ],
)
def test_declared_parameters_reachable_via_kwargs(label: str, toolspec: Any) -> None:
    """A ToolSpec that declares parameters must read them from kwargs.

    Native (tool-format) calls deliver all parameters exclusively through
    kwargs; an executor that ignores kwargs silently drops native input.
    Specs using only ``functions=`` are exempt (gptme handles mapping there).
    """
    from gptme.tools.base import ToolSpec

    assert isinstance(toolspec, ToolSpec)
    assert toolspec.execute is not None
    assert toolspec.parameters

    # Exempt specs that don't define a custom executor body (e.g. lambda wrappers)
    try:
        source = inspect.getsource(toolspec.execute)
    except (OSError, TypeError):
        pytest.skip(f"Cannot retrieve source for {label}")

    assert _reads_kwargs(source), (
        f"{label}: ToolSpec declares {[p.name for p in toolspec.parameters]!r} "
        "but the executor never reads from kwargs. "
        "Native calls deliver parameters only through kwargs — they will be silently ignored."
    )
