# gptme-hooks-examples — template plugin for writing gptme lifecycle hooks

A minimal, copyable gptme plugin that registers hooks at session start, before
tool execution, and after each turn. Use it as a starting point for your own
hook plugin (guardrails, logging/analytics, context injection, ...).

**Status:** example / template. Not meant to be enabled in day-to-day use — it
posts a system message on every hook it handles.

## What it shows

- Plugin layout with a `hooks/` package and a `register()` function
- Registering hooks with `register_hook(name, hook_type, func, priority)`
- Yielding `Message`s from a hook
- Priorities (higher runs first)

> **Heads-up:** `tool_pre_execute_hook` in `example_hooks.py` still uses the
> older `(log, workspace, tool_use)` signature. Current gptme calls
> `TOOL_EXECUTE_PRE` hooks with a single `ToolExecutePreData` argument (see
> [signatures](#hook-signatures) below). For an up-to-date tool hook, see
> [gptme-action-receipts](../gptme-action-receipts/README.md).

For real-world hook plugins in this repo, see
[gptme-action-receipts](../gptme-action-receipts/README.md) (`TOOL_EXECUTE_PRE`
audit + gate) and
[gptme-headroom-compressor](../gptme-headroom-compressor/README.md)
(`GENERATION_PRE` context rewriting).

## Layout

```text
gptme-hooks-examples/
├── pyproject.toml
├── src/gptme_example_hooks/
│   ├── __init__.py
│   └── hooks/
│       ├── __init__.py          # re-exports register()
│       └── example_hooks.py     # hook functions + register()
└── tests/test_example_hooks.py
```

## Try it

The package has no entry point; load it as a folder plugin. Point
`[plugins] paths` at **this plugin's directory** (gptme then finds
`src/gptme_example_hooks/hooks/` and calls its `register()`):

```toml
# gptme.toml or ~/.config/gptme/config.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-hooks-examples"]
enabled = ["gptme_example_hooks"]   # only needed if you use an allowlist
```

Pointing `paths` at the parent `plugins/` directory is not enough for hook
plugins: src-layout plugins found that way only contribute tools.

## Writing a hook

```python
from gptme.hooks import HookType, StopPropagation, register_hook
from gptme.message import Message


def session_start_hook(logdir, workspace, initial_msgs):
    yield Message("system", f"Plugin loaded in {workspace}")


def warn_rm_rf(data):  # TOOL_EXECUTE_PRE: data is a ToolExecutePreData
    tool_use = data.tool_use
    if tool_use and tool_use.tool == "shell" and "rm -rf" in (tool_use.content or ""):
        yield Message("system", "Warning: destructive command about to run")
        yield StopPropagation()  # skip lower-priority TOOL_EXECUTE_PRE hooks


def register():
    register_hook("my_plugin.session_start", HookType.SESSION_START,
                  session_start_hook, priority=100)
    register_hook("my_plugin.warn_rm_rf", HookType.TOOL_EXECUTE_PRE,
                  warn_rm_rf, priority=10)
```

Hooks are generators; a hook that only observes can `return` without yielding.
Yielded `Message`s are added to the conversation. `StopPropagation()` (no
arguments) stops lower-priority hooks of the same type; it does **not** cancel
the tool call itself. Allowing or skipping a tool call goes through gptme's
confirmation mechanism (`TOOL_CONFIRM` hooks returning a `ConfirmationResult`).

## Hook signatures

| Hook type(s) | Called with |
|--------------|-------------|
| `SESSION_START` | `(logdir, workspace, initial_msgs)` |
| `SESSION_END`, `STEP_PRE`, `TURN_POST`, `MESSAGE_TRANSFORM` | `(manager)` — a `LogManager` |
| `TOOL_EXECUTE_PRE` / `TOOL_EXECUTE_POST` | `(data)` — `ToolExecutePreData` / `ToolExecutePostData` with `.log`, `.workspace`, `.tool_use` (and `.result_msgs` for post) |
| `GENERATION_PRE` | `(messages, **kwargs)` — kwargs include `workspace`, `model` |
| `GENERATION_POST` | `(message, **kwargs)` |
| `CACHE_INVALIDATED` | `(manager, reason, tokens_before, tokens_after)` |

All hook types (`gptme.hooks.HookType`): `STEP_PRE`, `STEP_POST`,
`TURN_PRE`, `TURN_POST`, `MESSAGE_TRANSFORM`, `TOOL_EXECUTE_PRE`,
`TOOL_EXECUTE_POST`, `TOOL_TRANSFORM`, `FILE_SAVE_PRE`, `FILE_SAVE_POST`,
`FILE_PATCH_PRE`, `FILE_PATCH_POST`, `SESSION_START`, `SESSION_END`,
`GENERATION_PRE`, `GENERATION_POST`, `GENERATION_CHUNK`,
`GENERATION_INTERRUPT`, `LOOP_CONTINUE`, `CWD_CHANGED`, `CACHE_INVALIDATED`,
`TOOL_CONFIRM`, `ELICIT`. The authoritative signatures are the Protocol classes
in [`gptme/hooks/types.py`](https://github.com/gptme/gptme/blob/master/gptme/hooks/types.py).

## Making your own

1. Copy this directory and rename the package (`gptme_example_hooks` →
   `gptme_your_plugin`) and the project name in `pyproject.toml`.
2. Put hook functions in `src/<package>/hooks/*.py` and register them in
   `register()`.
3. Optionally make it pip-installable as a unified plugin by exporting a
   `GptmePlugin(name=..., register_hooks=register)` and adding a
   `[project.entry-points."gptme.plugins"]` entry, as
   [gptme-action-receipts](../gptme-action-receipts/) does.

## Resources

- [gptme hooks docs](https://gptme.org/docs/hooks.html)
- [gptme plugin docs](https://gptme.org/docs/plugins.html)

## License

MIT
