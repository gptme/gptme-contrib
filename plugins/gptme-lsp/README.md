# gptme-lsp

Language Server Protocol (LSP) integration for [gptme](https://gptme.org): gives the agent IDE-grade code intelligence — diagnostics, go-to-definition, find-references, hover/type info, call hierarchy, and rename previews — by talking to the same language servers your editor uses.

**Status:** experimental. Works with Python, TypeScript/JavaScript, Go and Rust.

## Why use it

Plain `grep` and file reads tell an agent *where text is*; a language server tells it *what the code means*. With this plugin the agent can check a file for type errors before and after editing it, resolve a symbol to its definition, list every caller of a function, and preview a project-wide rename. A post-save hook also surfaces new errors right after the agent writes a file, so mistakes get caught in the same turn.

## Install

The plugin is not published to PyPI. Point gptme at the plugin directory in `gptme.toml` (project) or `~/.config/gptme/config.toml` (user):

```toml
[plugins]
paths = ["/path/to/gptme-contrib/plugins/gptme-lsp"]
enabled = ["gptme_lsp"]
```

Pointing at the plugin directory itself (not the parent `plugins/` folder) loads both the `lsp` tool and the post-save diagnostics hook.

Then install the language servers you need and make sure they are on `PATH`:

| Language | Extensions | Server binary | Install |
|----------|------------|---------------|---------|
| Python | `.py`, `.pyi` | `pyright-langserver` | `pip install pyright` or `npm i -g pyright` |
| TypeScript / JavaScript | `.ts`, `.tsx`, `.js`, `.jsx` | `typescript-language-server` | `npm i -g typescript-language-server typescript` |
| Go | `.go` | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| Rust | `.rs` | `rust-analyzer` | `rustup component add rust-analyzer` |

## Quickstart

Ask gptme to use it:

```sh
gptme "run lsp diagnostics on src/main.py and fix any errors"
```

Inside a session, `/lsp` (or `/lsp status`) shows which servers are available. The agent invokes the tool with an `lsp` block, for example:

````
```lsp
diagnostics src/main.py
```
````

Positions use `file:line:col` or `file:line` (1-based). The workspace root is the git root of the current directory.

## Actions

| Action | Arguments | What it does |
|--------|-----------|--------------|
| `status` | — | Show which language servers are installed |
| `diagnostics` | `<file>` | Errors and warnings for a file |
| `check` | — | Diagnostics for every file changed according to `git` |
| `definition` | `<file:line:col>` | Where a symbol is defined |
| `references` | `<file:line:col>` | All references to a symbol |
| `hover` | `<file:line:col>` | Type information and docs |
| `signature` | `<file:line:col>` | Function signature help |
| `rename` | `<file:line:col> <new_name>` | Preview a project-wide rename |
| `format` | `<file>` | Preview formatting edits |
| `actions` | `<file:line:col>` | Available code actions / quick fixes |
| `symbols` | `<query>` | Search workspace symbols |
| `hints` | `<file> [start:end]` | Inlay hints (parameter names, inferred types) |
| `callers` / `callees` | `<file:line:col>` | Incoming / outgoing call hierarchy |
| `tokens` | `<file> [start:end]` | Semantic tokens |
| `links` | `<file>` | Document links (URLs, file paths) |
| `lens` | `<file>` | Code lenses (e.g. reference counts) |

`rename` and `format` only **preview** edits; they do not modify files. The agent applies the edits afterwards with its normal editing tools.

### Post-save hook

After gptme saves a file with a supported extension, the plugin runs a quick diagnostics pass and reports any errors in the conversation.

## Configuration

Override or add server commands under `[plugin.lsp.servers]` in `~/.config/gptme/config.toml` or the project's `gptme.toml` (project settings win):

```toml
[plugin.lsp.servers]
python = ["pylsp"]
go = ["/custom/path/to/gopls", "serve"]
```

Built-in defaults are `pyright-langserver --stdio`, `typescript-language-server --stdio`, `gopls` and `rust-analyzer`.

> **Current limitation:** custom server commands are only used by the `tokens`, `links` and `lens` actions. The other actions and the post-save hook always use the built-in defaults, and only the four languages above are mapped to file extensions.

## Development

```sh
# from the gptme-contrib repo root
uv run pytest plugins/gptme-lsp/tests
```

## Related

- [gptme plugin docs](https://gptme.org/docs/plugins.html)
- [Other gptme-contrib plugins](../README.md)
