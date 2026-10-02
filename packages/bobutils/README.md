# bobutils

Small, stdlib-only helper functions shared by agent workspace scripts: JSONL
loading, ISO datetime and duration parsing, slugs, repo-root lookup, atomic
symlink-safe writes, GFM table escaping, and a public/private text sanitizer.

**Status:** internal helper library (`0.1.0`), extracted from Bob's workspace to
replace many drifting copy-pasted helpers. No stability guarantees and no
PyPI release; other agents can use it, but expect it to change. Some modules
(notably `public_safe`) encode Bob-specific patterns.

## Install

It has no runtime dependencies. Inside the gptme-contrib uv workspace it is a
workspace member (`uv sync --all-packages`). Standalone:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/bobutils"
```

## Modules

There are no top-level re-exports; import from the submodule.

| Module | API | Notes |
|--------|-----|-------|
| `bobutils.jsonl` | `iter_jsonl(path, *, encoding, on_invalid, must_exist)`, `load_jsonl(..., tail=None)` | Yields dict rows; skips blank lines; `on_invalid` is `"skip"` (default), `"warn"` or `"raise"`; missing file reads as empty unless `must_exist=True`; `.gz` handled transparently; `tail=N` keeps the last N rows |
| `bobutils.datetimes` | `parse_datetime(value, *, assume_utc=True)`, `UTC` | ISO 8601 (incl. `Z` suffix) to aware `datetime`; returns `None` instead of raising |
| `bobutils.durations` | `parse_duration(value, *, default_unit=None)` | `<int><unit>` with unit `s/m/h/d/w` to `timedelta`; bare integers only accepted with `default_unit` |
| `bobutils.coerce` | `coerce_int(value, default=0)`, `coerce_int_nullable(value)` | Lenient int coercion (None/bool give the default) |
| `bobutils.slugs` | `slugify(text, *, max_len=None, fallback="item")` | Lowercase hyphenated slug |
| `bobutils.roots` | `find_repo_root(cwd=None)` | `git rev-parse --show-toplevel`; raises `RuntimeError` on failure |
| `bobutils.shell` | `run_cmd(cmd, timeout=30, *, cwd=None)` | Returns stripped stdout, or `""` on failure |
| `bobutils.safe_write` | `safe_write(path, data, *, mode=0o644)`, `safe_write_bytes(...)` | Atomic write that resolves symlinks first, so a symlinked file is updated in place instead of being replaced by a regular file |
| `bobutils.markdown` | `escape_table_cell(text)`, `split_table_row_cells(line)` | Escape `\|` in GFM table cells, and split rows using the same rules |
| `bobutils.public_safe` | `public_safe(text)`, `validate_public_safe(text)`, `PublicSafeViolation` | Replaces private workspace paths and internal host names with placeholders before text is published; the patterns are specific to Bob's deployment |

## Example

```python
from bobutils.jsonl import load_jsonl
from bobutils.durations import parse_duration
from bobutils.safe_write import safe_write

rows = load_jsonl("state/events.jsonl", on_invalid="warn", tail=100)
window = parse_duration("7d")  # timedelta(days=7)
safe_write("state/summary.txt", f"{len(rows)} events in the last {window.days} days\n")
```

## Development

```bash
make test       # pytest
make typecheck  # mypy
```
