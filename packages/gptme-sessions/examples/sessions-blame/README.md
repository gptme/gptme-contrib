# sessions-blame demo

A self-contained, runnable demo of [`gptme-sessions blame`](../../README.md)
— the tool that answers *"which AI session produced this line / commit?"* by
correlating git author-dates with session time-windows.

## What it shows

`blame` takes a file path and a session-records store, then attributes each
commit touching that file to the session whose time-window contains the commit's
author-date. This demo wires up a minimal, deterministic version of that:

- **`session-records.jsonl`** — one sample session (`demo-sess-001`) with a
  window of `[10:00, 10:30]` UTC on `2026-06-01`.
- **`demo.sh`** — creates a throwaway git repo, makes a single commit authored
  at `10:15` UTC (inside the window), then runs `gptme-sessions blame` against
  the sample store.

The output shows the commit attributed to `demo-sess-001` with
`method=commit-window` — the exact mechanism the tool uses on real agent
workspaces.

## Run it

```bash
# from this directory
./demo.sh            # text output
./demo.sh --json     # also print the JSON form
```

Requires `git` and `gptme-sessions`:

```bash
pip install gptme-sessions
```

## Expected output

```
== commit ==
a3bb5de  2026-06-01T10:15:00+00:00  feat: add hello function

== gptme-sessions blame hello.py --records session-records.jsonl ==
Session provenance for hello.py

  ● 2026-06-01 10:15  a3bb5de83  session=demo-sess-001
      category=code  model=claude-sonnet-4-6  productivity=0.85  method=commit-window
      feat: add hello function
      journal: journal/2026-06-01/session.md

  ● exact (commit-window/trajectory/trailer)  ◐ ambiguous (multiple windows)  ○ nearest (≤30m)  · unattributable
```

(The commit SHA will differ on each run — the attribution line is what matters.)

## How it works

1. `demo.sh` builds a throwaway repo and commits `hello.py` with a pinned
   author-date of `2026-06-01T10:15:00+00:00`.
2. `gptme-sessions blame hello.py --records session-records.jsonl` reads the
   sample store, loads `demo-sess-001`'s window `[10:00, 10:30]`, and finds the
   commit's author-date inside it.
3. The commit is attributed to `demo-sess-001` with `method=commit-window`.

On a real workspace the same command reads the live `state/sessions/` store and
attributes every commit touching a file to the session that authored it — the
basis for `scripts/analysis/sessions-blame.py` ("git blame for the AI era").
