# gptme-warpgrep

Natural-language code search for [gptme](https://gptme.org) using [Morph's warp-grep](https://docs.morphllm.com/sdk/components/warp-grep/direct) model. Ask "where are JWT tokens validated?" and get back the relevant code snippets with line numbers, instead of guessing regex patterns.

**Status:** experimental. Requires an API key for Morph (a third-party hosted service).

## How it works

warp-grep is a small search agent. For each query it explores your repository over up to 4 turns. The Morph model decides what to run; the plugin executes those operations **locally** against your checkout:

- `grep` searches for a regex, with `git grep` in a git repository or ripgrep (`rg`) otherwise,
- `read` reads a file or line range,
- `analyse` lists directory structure (git-tracked files only in a git repository),
- `finish` returns the final file and line ranges.

In a git repository only tracked files are searched, so `.gitignore` applies. Outside git, version-control directories, dependency folders (`node_modules`, `.venv`, `vendor`), caches, build output and lock files are excluded. File contents the model asks to see are sent to Morph's API (`api.morphllm.com`) as part of the search, so don't use it on code you can't share with that service.

Use it for conceptual or exploratory questions in unfamiliar codebases. For exact-symbol lookups, plain `grep`/`rg` or [gptme-lsp](../gptme-lsp/README.md) are faster and free.

## Install

1. To search directories that are not git repositories, install [ripgrep](https://github.com/BurntSushi/ripgrep) so `rg` is on `PATH` (git repositories only need `git`).
2. Point gptme at the plugin in `gptme.toml` (project) or `~/.config/gptme/config.toml` (user):

   ```toml
   [plugins]
   paths = ["/path/to/gptme-contrib/plugins/gptme-warpgrep"]
   enabled = ["gptme_warp_grep"]
   ```

   The plugin uses `httpx`, which must be importable in gptme's environment.
3. Set your Morph API key ([get one here](https://morphllm.com/dashboard)), either as an environment variable or in the `[env]` table of your gptme config:

   ```sh
   export MORPH_API_KEY="your-api-key"
   ```

## Quickstart

```sh
gptme "use warp_grep to find where database connection errors are handled"
```

The tool exposes one function, which the agent calls from a Python block:

```python
warp_grep("Find authentication middleware")
warp_grep("Find all API endpoints", "/path/to/project")   # search another repo
```

It returns Markdown with one fenced, syntax-highlighted snippet per matching file, or `No relevant code found for the query.`

### As a library

```python
from gptme_warp_grep.tools.warp_grep import warp_grep_search

results = warp_grep_search(
    query="Find where JWT tokens are validated",
    repo_root="/path/to/project",
    # api_key="...",  # defaults to MORPH_API_KEY
)
for f in results:  # list of ResolvedFile(path, content)
    print(f"=== {f.path} ===")
    print(f.content)
```

## Agent lesson

[`lessons/tools/warp-grep-usage.md`](lessons/tools/warp-grep-usage.md) is a keyword-triggered lesson that teaches an agent when to reach for warp-grep. Copy it into your agent's lessons directory if you use the gptme lesson system.

## Development

```sh
cd plugins/gptme-warpgrep
make test        # uv run pytest tests/ -v
make typecheck   # uv run mypy src/ --ignore-missing-imports
```

## License

MIT
