"""Conformance guard for plugin tools in the native tool-call format.

A native ("tool" format) call delivers its input as ``kwargs`` only:
``code`` and ``args`` are ``None`` (gptme/gptme#4463). A plugin tool that
builds its input any other way silently drops every native call.

gptme core guards its own tools with
``tests/test_tools_parameter_conformance.py`` (gptme/gptme#4366, extended in
gptme/gptme#4469). This file is the same guard for the plugin tools in this
repo, because the plugin surface is where both known instances of the class
were found:

- gptme/gptme-contrib#1967: ``lsp`` and ``youtube`` had a *required* fourth
  positional argument (``confirm``) that gptme's dispatcher never passes, so
  every call failed with ``execute() missing 1 required positional argument``.
- ``lsp`` also declared ``action``/``target`` parameters but did not read
  them, so a native call had no expressible input.

Two checks:

1. **Executor shape** — a spec with a custom executor must be callable with
   the dispatcher's shape ``execute(content, args, kwargs)``.
2. **Declared-parameter reachability** — a spec that declares ``parameters``
   must read them from ``kwargs``; a native call carries no ``code``/``args``.

Specs that only use ``functions=[...]`` are dispatched through gptme's
``from_function`` path, which maps native ``kwargs`` onto the function
signature, so they are exempt from (2).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
from gptme.tools.base import ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Specs whose declared parameters are not read from kwargs, with the reason.
#: Keep entries explicit so an intentional exception is visible in the diff.
_ALLOWLIST: dict[str, set[str]] = {}


@dataclass
class PluginSpec:
    """A ToolSpec found in a plugin, with the module it was found in."""

    module: str
    name: str
    spec: ToolSpec

    @property
    def key(self) -> str:
        return f"{self.module}.{self.name}"


def _plugin_package_names() -> list[str]:
    """Importable package names of the plugins in this repo."""
    names = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        src = plugin_dir / "src"
        if not (plugin_dir.is_dir() and src.is_dir()):
            continue
        for pkg_dir in sorted(src.iterdir()):
            if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
                names.append(pkg_dir.name)
    return names


def _iter_plugin_specs() -> Iterator[PluginSpec]:
    """Yield every ToolSpec defined by a plugin in this repo."""
    seen: set[int] = set()
    for pkg_name in _plugin_package_names():
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception:  # pragma: no cover - optional dependency missing
            continue
        modules = [pkg]
        if hasattr(pkg, "__path__"):
            for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
                try:
                    modules.append(importlib.import_module(info.name))
                except Exception:  # pragma: no cover - optional dependency missing
                    continue
        for module in modules:
            for obj in vars(module).values():
                if not isinstance(obj, ToolSpec) or id(obj) in seen:
                    continue
                seen.add(id(obj))
                yield PluginSpec(module=module.__name__, name=obj.name, spec=obj)


def _signature_violation(execute: Any) -> str | None:
    """Return why ``execute`` cannot be called as ``(content, args, kwargs)``.

    gptme's dispatcher calls ``tool.execute(self.content, self.args,
    self.kwargs)`` — three positional arguments. An executor with an extra
    *required* positional parameter raises at call time, in every format.
    """
    try:
        inspect.signature(execute).bind(None, None, {})
    except (TypeError, ValueError) as exc:
        return str(exc)
    return None


def _is_kwargs_expr(node: ast.expr) -> bool:
    """Match ``kwargs`` and ``(kwargs or {})``."""
    if isinstance(node, ast.Name):
        return node.id == "kwargs"
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and bool(node.values)
        and isinstance(node.values[0], ast.Name)
        and node.values[0].id == "kwargs"
    )


def _kwargs_keys_read(func: Any) -> set[str] | None:
    """Literal ``kwargs`` keys read by ``func``, or None if undeterminable.

    None means "cannot tell statically" (no source available, or the function
    touches ``kwargs`` without a literal key) — callers must not treat that as
    a violation.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return None

    funcdef = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)),
        None,
    )
    if funcdef is None:
        return None

    keys: set[str] = set()
    touches_kwargs = False
    for node in ast.walk(funcdef):
        if isinstance(node, ast.Name) and node.id == "kwargs":
            touches_kwargs = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _is_kwargs_expr(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "kwargs"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)

    if not touches_kwargs:
        # The executor never looks at kwargs: a native call carries no other
        # input, so any declared parameter is unreachable.
        return set()
    if not keys:
        # kwargs is used, but not with literal keys: cannot judge statically.
        return None
    return keys


def _unreachable_params(plugin_spec: PluginSpec) -> set[str]:
    """Declared parameters the executor cannot receive from a native call."""
    spec = plugin_spec.spec
    declared = {p.name for p in (spec.parameters or [])}
    if not declared or spec.functions:
        # Function-dispatched specs are routed through gptme's from_function
        # path, which maps native kwargs onto the function signature.
        return set()
    declared -= _ALLOWLIST.get(plugin_spec.key, set())
    if not declared:
        return set()
    execute = spec.execute
    if execute is None:
        # No custom executor: the spec is not callable as a tool at all.
        return set()
    keys = _kwargs_keys_read(execute)
    if keys is None:
        return set()
    return declared - keys


_SPECS = list(_iter_plugin_specs())
_EXECUTOR_SPECS = [ps for ps in _SPECS if ps.spec.execute is not None]


def test_plugin_specs_were_discovered() -> None:
    """Guard against a vacuous suite: plugin specs must be found.

    A discovery regression (renamed package, changed layout) would otherwise
    leave the checks below passing with nothing to check.
    """
    assert _EXECUTOR_SPECS, (
        "no plugin ToolSpec with a custom executor was found - "
        "the conformance checks would be vacuous"
    )


@pytest.mark.parametrize(
    "plugin_spec",
    _EXECUTOR_SPECS,
    ids=lambda ps: ps.key,
)
def test_plugin_executor_accepts_native_call(plugin_spec: PluginSpec) -> None:
    """A plugin executor must be callable as ``execute(content, args, kwargs)``."""
    violation = _signature_violation(plugin_spec.spec.execute)
    assert violation is None, (
        f"{plugin_spec.key}: executor cannot be called as "
        f"execute(content, args, kwargs) - {violation}\n"
        "gptme's dispatcher passes exactly three positional arguments; an extra "
        "required parameter raises at call time in every format "
        "(see gptme/gptme-contrib#1967)."
    )


@pytest.mark.parametrize(
    "plugin_spec",
    [ps for ps in _SPECS if ps.spec.parameters],
    ids=lambda ps: ps.key,
)
def test_plugin_declared_params_reachable_from_kwargs(plugin_spec: PluginSpec) -> None:
    """Every declared parameter must be readable from kwargs.

    A native call delivers its input as kwargs only, so a parameter the
    executor never reads is inexpressible in that format (gptme/gptme#4463).
    """
    unreachable = _unreachable_params(plugin_spec)
    assert not unreachable, (
        f"{plugin_spec.key}: declared parameter(s) {sorted(unreachable)} are not "
        "read from kwargs, so a native tool call cannot express them\n"
        "Either read the parameter from kwargs (falling back to code/args) or "
        f"add it to _ALLOWLIST in {Path(__file__).name}."
    )


def test_guard_detects_a_broken_executor_shape() -> None:
    """The signature check must actually catch the #1967 shape."""

    def execute_with_extra_required(
        code: str | None,
        args: list[str] | None,
        kwargs: dict[str, str] | None,
        confirm: Any = None,
    ) -> str:
        return "ok"

    def execute_with_extra_required_param(
        code: str | None,
        args: list[str] | None,
        kwargs: dict[str, str] | None,
        confirm: Any,
    ) -> str:
        return "ok"

    assert _signature_violation(execute_with_extra_required) is None
    assert _signature_violation(execute_with_extra_required_param) is not None


def test_guard_detects_an_unread_declared_param() -> None:
    """The reachability check must catch a declared-but-unread parameter."""

    def execute_ignoring_kwargs(
        code: str | None,
        args: list[str] | None,
        kwargs: dict[str, str] | None,
    ) -> str:
        return (code or "").strip()

    def execute_reading_kwargs(
        code: str | None,
        args: list[str] | None,
        kwargs: dict[str, str] | None,
    ) -> str:
        return (kwargs or {}).get("action", "")

    assert _kwargs_keys_read(execute_ignoring_kwargs) == set()
    assert _kwargs_keys_read(execute_reading_kwargs) == {"action"}
