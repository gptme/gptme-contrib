"""Explicit environment contract for the runner subprocess boundary.

``execute_plan(..., runner_base_env=...)`` replaces the runner environment
instead of inheriting the ambient agent environment. This module builds that
mapping from a small, named contract.

Why it exists (ErikBjare/bob#1409): a project-monitoring slot is launched from
a shell wrapper that sources ``~/.profile``, which sources the workspace
``.env``. The session therefore inherits every credential the workspace owns —
banking, storage, social, telemetry — although a session only needs its own
inference credential and a handful of execution settings. Replacing the
environment at the runner boundary removes that unrelated ambient authority
without claiming filesystem isolation: credential files under ``HOME`` remain
readable to the same UID.

The builder is deliberately pure: no I/O, no credential retrieval, no
``os.environ`` mutation. A missing structural setting (``PATH``, ``HOME``) is a
hard error; a missing optional credential is simply absent, so the backend
fails closed on its own instead of falling back to an ambient source.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

#: Execution settings a backend launcher needs. Values are copied verbatim.
#: Keep this list to settings, never to application credentials.
DEFAULT_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "TMPDIR",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NPM_CONFIG_CACHE",
    "NPM_CONFIG_PREFIX",
    "GIT_CONFIG_GLOBAL",
    "GIT_SSH_COMMAND",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

#: Locale variants (``LC_ALL``, ``LC_CTYPE``, ...) are selected by prefix.
DEFAULT_PASSTHROUGH_PREFIXES: tuple[str, ...] = ("LC_",)

#: Settings whose absence makes a launch fail in confusing ways downstream.
DEFAULT_REQUIRED: tuple[str, ...] = ("PATH", "HOME")

#: Credentials each backend consumes from the environment. Backends that
#: authenticate through a file under ``HOME`` (Claude Code, Codex, Copilot,
#: Grok Build) need nothing here — their credential file stays readable to the
#: same UID, which this boundary does not isolate.
DEFAULT_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "gptme": ("OPENROUTER_API_KEY",),
    "pi": ("OPENROUTER_API_KEY", "XAI_API_KEY"),
}

#: python-dotenv honors this: set it and a backend cannot rehydrate a workspace
#: ``.env`` into its process environment (and from there into tool children).
DOTENV_DISABLE_VAR = "PYTHON_DOTENV_DISABLED"


class MissingRunnerEnv(RuntimeError):
    """A required non-secret setting is absent from the parent environment."""


def build_runner_base_env(
    environ: Mapping[str, str],
    backend: str,
    *,
    passthrough: Iterable[str] | None = None,
    prefixes: Iterable[str] | None = None,
    credentials: Iterable[str] | None = None,
    extra: Mapping[str, str] | None = None,
    require: Iterable[str] | None = DEFAULT_REQUIRED,
    disable_dotenv: bool = True,
) -> dict[str, str]:
    """Return the explicit runner environment for ``backend``.

    Args:
        environ: Parent environment to select from (normally ``os.environ``).
        backend: Backend name; selects the default credential names.
        passthrough: Non-secret setting names to copy. ``None`` uses
            :data:`DEFAULT_PASSTHROUGH`.
        prefixes: Name prefixes to copy (locale variants). ``None`` uses
            :data:`DEFAULT_PASSTHROUGH_PREFIXES`.
        credentials: Credential names to copy. ``None`` uses the backend
            default; ``[]`` copies none.
        extra: Additional fixed settings (e.g. ``BOB_BACKEND``). Values are
            stringified; an explicit value wins over a passthrough one.
        require: Names that must be present and non-empty in the result.
            ``None`` uses :data:`DEFAULT_REQUIRED`; ``()`` requires nothing.
        disable_dotenv: Set :data:`DOTENV_DISABLE_VAR` so a Python backend
            cannot load a workspace ``.env`` behind this boundary.

    Raises:
        MissingRunnerEnv: A name in ``require`` is absent or empty. The
            message lists names only, never values.
    """
    selected = set(passthrough if passthrough is not None else DEFAULT_PASSTHROUGH)
    name_prefixes = tuple(
        prefixes if prefixes is not None else DEFAULT_PASSTHROUGH_PREFIXES
    )

    env = {name: environ[name] for name in selected if name in environ}
    for name in sorted(environ):
        if name in env or not name_prefixes:
            continue
        if any(name.startswith(prefix) for prefix in name_prefixes):
            env[name] = environ[name]

    cred_names = (
        DEFAULT_CREDENTIALS.get(backend, ()) if credentials is None else credentials
    )
    for name in cred_names:
        if name in environ:
            env[name] = environ[name]

    if disable_dotenv:
        env[DOTENV_DISABLE_VAR] = "1"

    if extra:
        for name, value in extra.items():
            env[str(name)] = str(value)

    required = DEFAULT_REQUIRED if require is None else tuple(require)
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise MissingRunnerEnv(
            "runner environment is missing required settings: "
            + ", ".join(sorted(missing))
        )
    return env


def runner_env_from_spec(
    spec: Mapping[str, object],
    environ: Mapping[str, str] | None = None,
    *,
    backend: str,
) -> dict[str, str] | None:
    """Resolve a config table into a runner base environment.

    ``spec`` is the ``[config.runner_env]`` table. It must declare a ``mode``:

    - ``legacy``: return ``None`` so ``execute_plan`` inherits the ambient
      environment unchanged (the historical behavior; an explicit opt-out).
    - ``explicit``: build the contract. An unknown or missing mode is a
      configuration error, never a silent fallback.

    Recognized keys: ``mode``, ``passthrough``, ``prefixes``, ``credentials``,
    ``set``, ``require``, ``disable_dotenv``.
    """
    mode = spec.get("mode")
    if mode == "legacy":
        return None
    if mode != "explicit":
        raise ValueError("runner_env.mode must be 'legacy' or 'explicit'")

    def _names(key: str) -> list[str] | None:
        value = spec.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            raise ValueError(f"runner_env.{key} must be a list of names")
        if not isinstance(value, list | tuple):
            raise ValueError(f"runner_env.{key} must be a list of names")
        return [str(item) for item in value]

    extra = spec.get("set")
    if extra is not None and not isinstance(extra, Mapping):
        raise ValueError("runner_env.set must be a table of fixed values")

    require = _names("require")
    return build_runner_base_env(
        os.environ if environ is None else environ,
        backend,
        passthrough=_names("passthrough"),
        prefixes=_names("prefixes"),
        credentials=_names("credentials"),
        extra={str(k): str(v) for k, v in (extra or {}).items()},
        require=DEFAULT_REQUIRED if require is None else tuple(require),
        disable_dotenv=bool(spec.get("disable_dotenv", True)),
    )
