# gptme-headroom-compressor — lossless compression of large structured tool output

A gptme hook that shrinks large JSON/tabular tool outputs in the conversation
before each LLM call, using the SmartCrusher transform from
[`headroom-ai`](https://pypi.org/project/headroom-ai/). Cuts token usage on
agents that read a lot of structured command output without dropping data.

**Status:** experimental, off by default. Requires the optional `headroom-ai`
dependency and an explicit opt-in.

## Why / when to use it

Shell commands like `gh ... --json`, `jq`, `kubectl get -o json` or CSV dumps
produce verbose, repetitive output that stays in context for the rest of the
session. This plugin rewrites such messages into a compact equivalent at read
time (the stored conversation log is unchanged).

It is designed to run just before
[gptme-tooloutput-trimmer](../gptme-tooloutput-trimmer/) (priority 201 vs. 200):
the compressor handles structured output losslessly, and anything it leaves
untouched (unstructured text) falls through to the trimmer.

## Install

Not published on PyPI. Install into the same environment as gptme, with the
`headroom` extra:

```bash
pip install "gptme-headroom-compressor[headroom] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-headroom-compressor"
```

It registers a `gptme.plugins` entry point named `headroom_compressor` (add
that name to `[plugins] enabled` if you use an allowlist). Without
`headroom-ai` installed, the hook logs a warning and does nothing.

## Enable

Either in `gptme.toml` / `~/.config/gptme/config.toml`:

```toml
[plugin.headroom_compressor]
enabled = true
min_compress_chars = 2000          # only try messages at least this long
raw_tool_prefixes = ["cat ", "git diff"]   # shell commands to never compress
```

or with the environment variable `GPTME_HEADROOM_ENABLED=1` (overrides the
config file; `0` disables).

## What gets compressed

A message is considered only if it is an unpinned system message that starts
like a tool result (`Ran command: \`...\`` or `Executed code block.`), is at
least `min_compress_chars` long, hasn't been compressed already, and its
command doesn't match `raw_tool_prefixes`. The command line is kept, the
fenced output is passed to `SmartCrusher.crush()`, and if it changed, the
message is replaced with:

```text
[Headroom compressed (orig=18234, strategy=..., savings=62%)]
Ran command: `gh pr list --json ...`
...compressed output...
```

Per-session counts and characters saved are logged and, where supported,
recorded in gptme's cost tracker under `headroom_compressor`.
