---
title: Write a Custom Tool Plugin
description: Extend gptme with a domain-specific tool using the Python plugin API
category: custom-tools
difficulty: advanced
tags: [plugin, custom-tool, python-api, extension]
deep_link: "https://gptme.ai/?prompt=Help+me+write+a+gptme+plugin+that+exposes+a+%60query_db%60+tool+for+reading+from+my+local+SQLite+database."
---

# Write a Custom Tool Plugin

## Problem

gptme's built-in tools cover general-purpose tasks, but domain-specific
operations — querying a proprietary API, reading a custom binary format, calling
an internal service — require a custom tool. Without one, you're pasting raw
output into the prompt by hand, which is slow and error-prone.

## Solution

gptme has a plugin API. A plugin is a Python package that exposes a `GptmePlugin`
instance via the `gptme.plugins` entry-point group. Each tool in the plugin is a
`ToolSpec` with a name, description (shown to the LLM), and an `execute` function
that runs when the LLM calls the tool.

Install the plugin and the LLM can call your custom tool just like any built-in.

## Example

A plugin that lets gptme query a local SQLite database:

```python
# my_gptme_plugin/__init__.py
import sqlite3
from collections.abc import Generator
from gptme.tools.base import ToolSpec
from gptme.message import Message
from gptme.plugins.plugin import GptmePlugin


def _execute_query(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
) -> Generator[Message, None, None]:
    """Execute a read-only SQL query and yield results as a markdown table."""
    kw = kwargs or {}
    db_path = kw.get("db_path", "")
    sql = kw.get("sql", code or "")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    if not cols:
        yield Message("system", "(no results)")
        return
    header = " | ".join(cols)
    sep = " | ".join("---" for _ in cols)
    body = "\n".join(" | ".join(str(c) for c in row) for row in rows)
    yield Message("system", f"{header}\n{sep}\n{body}")


plugin = GptmePlugin(
    name="sqlite",
    tools=[
        ToolSpec(
            name="query_db",
            desc="Run a read-only SQL query against a local SQLite database and return results as a markdown table.",
            execute=_execute_query,
        )
    ],
)
```

```toml
# pyproject.toml (in the plugin package)
[project]
name = "my-gptme-plugin"
version = "0.1.0"

[project.entry-points."gptme.plugins"]
sqlite = "my_gptme_plugin:plugin"
```

Install and use:

```bash
pip install -e ./my_gptme_plugin

# Verify it's loaded
gptme --tools

# Use it in a session
gptme "Query the users table in /var/app/prod.db and find accounts created in the last 7 days"
```

gptme will call `query_db` with the right arguments and show you the results
inline, without you needing to copy-paste any SQL output.

## Notes

- Use `mode=ro` in the SQLite URI (as shown) to prevent accidental writes from
  a buggy SQL statement the LLM generates.
- Keep tool descriptions concise but precise — the LLM reads the `desc` field to
  decide when and how to call the tool. Poor descriptions lead to wrong calls.
- The `execute` function receives `(code, args, kwargs)` and yields `Message`
  objects. Use `kwargs` for named parameters passed from the LLM's tool call.
- For tools that perform writes or have side effects, add a confirmation prompt
  in the executor function and document it clearly in the `desc`.
- See [gptme plugin docs](https://gptme.org/docs/plugins.html) for the full
  `GptmePlugin` and `ToolSpec` API reference.
