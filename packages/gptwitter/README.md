# gptwitter

Twitter/X integration for gptme agents, packaged as a normal uv workspace
package instead of a pile of standalone scripts.

## Status

This is the first extraction slice from `scripts/twitter/`:

- `gptwitter.api` provides the shared Twitter client/auth helpers.
- `gptwitter.cli` exposes the existing Twitter CLI as package entry points.
- `scripts/twitter/twitter.py` remains as a backward-compatible wrapper.

The larger workflow refactor is still ongoing. `workflow.py` is still a
monolith, but it now imports the shared client layer from this package.

## Install

```sh
uv pip install -e packages/gptwitter
```

Or run directly from the workspace:

```sh
uv run gptwitter --help
```

## Commands

```sh
gptwitter me
gptwitter post "hello world"
gptwitter replies --since 24h
gptwitter timeline --since 7d
```
