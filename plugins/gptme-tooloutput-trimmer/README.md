# gptme-tooloutput-trimmer

Cut LLM token costs in long [gptme](https://gptme.org) sessions by trimming (or optionally summarizing) large, old tool outputs, such as shell command and Python execution results, right before each model request, at the moments when doing so won't break the prompt cache.

**Status:** experimental. Opt-in: does nothing until enabled.

## Why use it

In long agent sessions, old command output (test logs, file listings, build output) is re-sent to the model on every request long after it stopped being useful. Trimming it saves tokens, but trimming at the wrong time invalidates the provider's prompt cache and can cost *more*. This plugin only trims when the request is expected to miss the cache anyway, or when the context is getting too big.

## How it works

The plugin registers two `GENERATION_PRE` hooks. They rewrite the messages sent to the model; the conversation log itself is not modified.

**Trimmer** (priority 200). When enabled, it acts only if at least one trigger fires:

- **expected-cache-cold**: the model is a direct Anthropic model (`anthropic/…` or `claude-…`), an earlier request wrote to the prompt cache, and more than 5 minutes (the cache TTL) have passed since the last Anthropic request;
- **cache-invalidated**: gptme's cache-awareness tracking reports the cache was invalidated this turn;
- **context-pressure**: total message content exceeds `pressure_chars`.

It then replaces each eligible message with a short preview:

```text
[Tool output trimmed (orig=48213 chars); first 500 chars]
<first 500 chars of the original output>
```

A message is eligible if it is a non-pinned system message that starts with `Ran command: \`` or `Executed code block.`, is longer than `max_output_chars`, and comes before the last `recent_turns` assistant messages. Output from commands matching `raw_tool_prefixes` is never trimmed.

**Summarizer** (priority 202, runs first; off by default). When enabled, it takes the `summarize_window` most recent eligible tool outputs, asks gptme's default summary model for one summary of them, and replaces them with a single `[Summarized previous tool outputs]` message. Summaries are memoized per content and model, so repeated requests over the same history don't re-bill the summarizer. The summarizer is independent of the trimmer's triggers; if it fails, the outputs are left for the trimmer (when enabled) to handle.

It also works with [gptme-headroom-compressor](../gptme-headroom-compressor/) (priority 201): messages that compressor has already handled are skipped.

## Install

Install into the same Python environment as gptme. The package registers itself through the `gptme.plugins` entry point (as `tooloutput_trimmer`):

```sh
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tooloutput-trimmer"
# or, for a pipx-installed gptme:
pipx inject gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tooloutput-trimmer"
```

If you restrict plugins with `[plugins] enabled = [...]`, add `"tooloutput_trimmer"` to that list.

## Quickstart

Enable it in `~/.config/gptme/config.toml` or a project's `gptme.toml`:

```toml
[plugin.tooloutput_trimmer]
enabled = true
```

or for a single run:

```sh
GPTME_READ_TIME_TRIMMER=1 gptme "run the full test suite and fix the failures"
```

Each trim is logged at INFO level as `tooloutput_trimmer: trimmed N message(s), saved M chars (<triggers>)`.

## Configuration

`[plugin.tooloutput_trimmer]` keys (user config is merged with project config; project wins per key):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Turn the trimmer on |
| `max_output_chars` | `8000` | Only trim tool outputs longer than this |
| `recent_turns` | `5` | Never touch output from the last N assistant turns |
| `preview_chars` | `500` | Characters kept as a preview |
| `pressure_chars` | `100000` | Total message size that triggers trimming regardless of cache state |
| `raw_tool_prefixes` | `[]` | Shell command prefixes whose output is never trimmed, e.g. `["git diff", "cat "]` |
| `summarize` | `false` | Turn the LLM summarization pass on |
| `summarize_window` | `3` | Number of most recent eligible outputs folded into one summary |

Environment variables:

| Variable | Effect |
|----------|--------|
| `GPTME_READ_TIME_TRIMMER` | `1`/`true` or `0`/`false`; overrides `enabled` |
| `GPTME_SUMMARIZE_TOOL_OUTPUTS` | Overrides `summarize` |
| `GPTME_TRIM_BYPASS` | `1`/`true`/`yes`: skip both trimming and summarization (full output passes through) |

## Development

```sh
# from the gptme-contrib repo root
uv run pytest plugins/gptme-tooloutput-trimmer/tests
```

## Related

- [gptme-headroom-compressor](../gptme-headroom-compressor/): companion context compressor
- [gptme plugin docs](https://gptme.org/docs/plugins.html)
