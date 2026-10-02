# gptme-ace — Agentic Context Engineering for gptme lessons

Hybrid (keyword + semantic + effectiveness + recency) lesson retrieval and a
Generator → Reflector → Curator → Reviewer → Applier pipeline that turns agent
session logs into proposed lesson updates.

**Status:** experimental. Built and used inside an autonomous-agent workspace;
the APIs and on-disk layout (`insights/`, `deltas/`, `logs/`) may still change.
The LLM-backed stages call the Anthropic API directly.

## Why / when to use it

gptme ships a keyword/wildcard lesson matcher. ACE is for agents with a large
`lessons/` directory that want:

- **Better lesson recall** — semantic similarity catches lessons whose keywords
  don't literally appear in the conversation.
- **Lesson hygiene** — find near-duplicate lessons, clusters, and merge
  candidates via embeddings.
- **Lessons that improve themselves** — mine session trajectories for insights
  and turn them into reviewable ADD/REMOVE/MODIFY "deltas" against `lessons/`.

The approach follows the *Agentic Context Engineering* (ACE) paper
(Stanford/SambaNova/UC Berkeley, Oct 2025): treat the prompt as a living
playbook that is generated, reflected on, and curated incrementally, to avoid
brevity bias and context collapse from wholesale rewrites.

Related: [gptme-retrieval](../gptme-retrieval/README.md) (context retrieval),
[gptme-attention-tracker](../gptme-attention-tracker/README.md) (HOT/WARM/COLD
context tiers), and the lesson tooling in
[gptme-lessons-extras](../../packages/gptme-lessons-extras/).

## Install

Not published on PyPI. Install from the repository subdirectory into the same
environment as gptme:

```bash
pip install "gptme-ace @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-ace"

# Extras: semantic matching / dedup (sentence-transformers, faiss-cpu, scipy)
pip install "gptme-ace[embeddings] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-ace"
# Extras: LLM pipeline stages + charts (anthropic, plotext, python-dotenv)
pip install "gptme-ace[full] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-ace"
```

Inside a gptme-contrib checkout it is a uv workspace member
(`uv sync --all-packages`).

The package registers a `gptme.plugins` entry point (`gptme_ace`). Enabling it
in gptme adds an `ace` tool whose only effect is to put usage instructions for
the pipeline into the system prompt — it does not replace gptme's lesson
matcher on its own.

## Quickstart

### Find duplicate / similar lessons

The embedder CLI runs via `python -m` (requires the `embeddings` extra). It
takes no path options: it reads `lessons/` and writes `embeddings/lessons/`
at the root of the gptme-contrib checkout the package was loaded from (the
paths are resolved relative to the package source, not the current
directory). So the CLI is only useful from a source checkout. For your own
lessons directory, use the Python API below.

```bash
python -m gptme_ace.embedder generate          # build embeddings (--force to rebuild)
python -m gptme_ace.embedder search "git worktree cleanup" --top-k 5
python -m gptme_ace.embedder duplicates --threshold 0.7
python -m gptme_ace.embedder suggest-merges
python -m gptme_ace.embedder dashboard
```

Other subcommands: `update`, `rebuild`, `similar --lesson-id ID`, `list`,
`cluster`, `check-new FILE`, `preview-merge LESSON1 LESSON2`.

### Hybrid matching from Python

```python
from pathlib import Path
from gptme_ace import GptmeHybridMatcher, LessonEmbedder

embedder = LessonEmbedder(
    lessons_dir=Path("lessons"),
    embeddings_dir=Path(".gptme/embeddings/lessons"),
)
matcher = GptmeHybridMatcher(embedder=embedder)

# lessons: gptme Lesson objects; context: gptme MatchContext
results = matcher.match(lessons, context, threshold=0.5)
```

`GptmeHybridMatcher` mirrors gptme's `LessonMatcher.match()` interface and
returns gptme `MatchResult`s. It only uses hybrid scoring when
`GPTME_LESSONS_HYBRID=true` (or `1`/`yes`) is set **and** an embedder is
provided; otherwise it falls back to keyword matching.

Default weights (`HybridConfig`): keyword 0.25, semantic 0.40, effectiveness
0.25, recency 0.10, plus a 0.20 bonus when a lesson's `tools` match the tools in
use.

> Note: gptme core has its own opt-in hybrid switch
> (`GPTME_LESSONS_USE_HYBRID`), but it currently imports the legacy module name
> `ace` rather than `gptme_ace`, so it does not pick up this package. Use the
> API above directly.

## Curation pipeline

Each stage is a Click CLI run with `python -m gptme_ace.<module>`. LLM stages
need `ANTHROPIC_API_KEY`; the model defaults to gptme's configured Anthropic
model and can be overridden with `GPTME_ACE_MODEL`.

| Stage | Command | Does |
|-------|---------|------|
| Generator | `generator analyze LOG [-o insights.json] [--dry-run] [--workspace DIR] [--no-lessons]` | Extracts thought-action-observation chains from a session log and proposes insights |
| Reflector | `reflector analyze INSIGHTS.json -o patterns.json` / `reflector refine INSIGHTS.json --patterns-file patterns.json -o refined.json` | Finds cross-insight patterns and refines insights |
| Storage | `storage list` / `storage show --id ID` / `storage stats` / `storage update-status --id ID --new-status S` | Inspect stored insights under `<workspace>/insights/` |
| Curator | `curator generate --insight-id ID` / `curator batch [--status approved] [--limit N]` / `curator list [--status pending\|approved\|rejected]` | Turns insights into lesson deltas under `./deltas/` |
| Reviewer | `reviewer review --delta-id ID [--auto-approve]` / `reviewer batch --all` / `reviewer approve\|reject --delta-id ID` / `reviewer status` | Scores deltas before they touch lessons |
| Applier | `applier apply --delta-id ID [--dry-run]` / `applier batch [--dry-run]` / `applier status` | Applies approved deltas to `lessons/` |
| Metrics | `python -m gptme_ace.metrics [WORKSPACE]` | Health summary from `<workspace>/logs/ace_curation_metrics.db` |

Insight storage resolves its workspace root from `AGENT_WORKSPACE` (absolute
path), then by walking up to a directory containing both `gptme.toml` and
`gptme-contrib/`, then `$HOME/<username>`. Curator, reviewer and applier work relative to the current
directory (`./deltas`, `./lessons`), so run them from your agent workspace root.

### Exploring results: `ace-viz`

The package installs an `ace-viz` console script:

```bash
ace-viz dashboard
ace-viz deltas list --status pending      # also: deltas show ID, deltas summary
ace-viz metrics runs --days 7             # also: metrics quality / impact / trends
ace-viz insights list
```

All subcommands accept `--json-output/-j`; the group accepts `--data-dir`.

## Python API

Exported from `gptme_ace`: `GptmeHybridMatcher`, `HybridLessonMatcher`,
`HybridConfig`, `LessonEmbedder`, `GeneratorAgent`, `TrajectoryParser`,
`ReflectorAgent`, `InsightStorage`, `CuratorAgent`, `Delta`, `DeltaReviewer`,
`DeltaApplier`, `MetricsDB`, `MetricsCalculator`, `get_default_metrics_db`.

## License

MIT
