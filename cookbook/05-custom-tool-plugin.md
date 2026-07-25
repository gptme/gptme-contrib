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

gptme has a plugin API. A plugin is a Python package that exports a `get_tools()`
function returning a list of `ToolSpec` objects. Each tool has a name, description
(shown to the LLM), parameters, and an executor function that runs when the LLM
calls the tool.

Install the plugin, point gptme at it, and the LLM can call your custom tool
just like any built-in.

## Example

A plugin that lets gptme query a local SQLite database:

```python
# my_gptme_plugin/__init__.py
import sqlite3
from gptme.tools import ToolSpec, ToolCall

def query_sqlite(db_path: str, sql: str) -> str:
    """Execute a read-only SQL query and return results as a markdown table."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    if not cols:
        return "(no results)"
    header = " | ".join(cols)
    sep = " | ".join("---" for _ in cols)
    body = "\n".join(" | ".join(str(c) for c in row) for row in rows)
    return f"{header}\n{sep}\n{body}"

def get_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="query_db",
            desc="Run a read-only SQL query against a local SQLite database and return results as a markdown table.",
            args={
                "db_path": ("string", "Absolute path to the SQLite database file"),
                "sql": ("string", "SQL SELECT statement to execute"),
            },
            fn=lambda args, **_: query_sqlite(args["db_path"], args["sql"]),
        )
    ]
```

```toml
# pyproject.toml (in the plugin package)
[project]
name = "my-gptme-plugin"
version = "0.1.0"

[project.entry-points."gptme.tools"]
query_db = "my_gptme_plugin"
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
- For tools that perform writes or have side effects, add a confirmation prompt
  in the executor function and document it clearly in the `desc`.
- See [gptme plugin docs](https://gptme.org/docs/plugins.html) for the full
  `ToolSpec` API reference.
