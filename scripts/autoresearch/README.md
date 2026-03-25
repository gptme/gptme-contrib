# Autoresearch

Iterative eval-gated code improvement loop inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch).

The loop:
1. Agent proposes a code change to the artifact (e.g. gptme)
2. Eval suite runs and produces a scalar score
3. If score improved: keep the change (git commit)
4. If score regressed: discard the change (git checkout)
5. Repeat for N iterations

When cumulative improvement exceeds a threshold, the loop auto-creates a PR.

## Architecture

```text
autoresearch-loop.sh          # Outer loop: budget management, experiment config
  └── merge-reject-loop.sh    # Core loop: propose → eval → keep/reject
        └── eval-score.sh     # Eval runner: gptme eval or custom command
```

### Supporting scripts

- `check-autoresearch-status.py` — Operator dashboard: branches, scores, budget
- `config.yaml` — Global budget config (shared across all experiments)

## Quick Start

```bash
# 1. Set up your artifact repo
export ARTIFACT_DIR=/path/to/gptme

# 2. Copy the example experiment config
cp scripts/autoresearch/examples/gptme-eval.yaml \
   scripts/autoresearch/experiments/my-experiment.yaml

# 3. Edit the config (set artifact_dir, model_candidates, etc.)
vim scripts/autoresearch/experiments/my-experiment.yaml

# 4. Run the loop
./scripts/autoresearch/autoresearch-loop.sh my-experiment
```

## Experiment Config

Experiments are YAML files in `experiments/`. Key fields:

| Field | Description | Default |
|-------|-------------|---------|
| `suite` | Eval suite name | (required) |
| `artifact_dir` | Path to repo being improved | current directory |
| `agent_harness` | Agent runner (`gptme` or `claude-code`) | `gptme` |
| `eval_cmd` | Custom eval command (stdout → float 0.0-1.0) | gptme eval |
| `program_spec` | Agent instruction file | `gptme-eval-program.md` |
| `model_candidates` | Comma-separated model list for agent | (required) |
| `eval_model_candidates` | Comma-separated model list for eval | (required) |
| `period_budget` | Max iterations per 24h | `15` |
| `total_budget` | All-time iteration limit (experiment exits when reached) | unlimited |
| `publish_threshold` | Min score delta to auto-create PR | `0.05` |
| `diagnosis_after_stuck_iters` | Self-diagnose after N consecutive rejections | `5` |
| `use_worktree` | Use git worktree for isolation | `1` |
| `enabled` | Enable/disable experiment | `true` |

### Custom Eval Commands

For non-gptme artifacts, set `eval_cmd` to a shell command that:
- Runs in `artifact_dir` with `MODEL` env var set
- Outputs a single float (0.0-1.0) to stdout
- Exits 0 on success, non-zero on failure

Example:
```yaml
eval_cmd: bash -c 'make test 2>/dev/null | tail -1 | grep -oP "\\d+\\.\\d+"'
```

## Budget System

Three levels of budget control:

1. **Per-experiment daily** (`period_budget`): Max iterations per experiment per day
2. **Global daily** (`config.yaml` → `global_daily_budget`): Max across ALL experiments
3. **All-time total** (`total_budget`): Experiment exits when lifetime limit reached

Budget state is stored in `state/autoresearch/budget/` as JSON files.

## Running as a Service

The outer loop (`autoresearch-loop.sh`) is designed to run as a systemd service:

```ini
[Unit]
Description=Autoresearch: %i
After=network.target

[Service]
Type=simple
ExecStart=%h/path/to/scripts/autoresearch/autoresearch-loop.sh %i
Restart=on-failure
RestartSec=300

[Install]
WantedBy=default.target
```

Install with: `systemctl --user enable --now autoresearch@my-experiment.service`

## Monitoring

```bash
# Check status (branches, scores, budget)
ARTIFACT_DIR=/path/to/gptme \
AUTORESEARCH_STATE_DIR=./state/autoresearch \
  python3 scripts/autoresearch/check-autoresearch-status.py

# View budget consumption
cat state/autoresearch/budget/*.json | python3 -m json.tool
```

## Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `ARTIFACT_DIR` | all scripts | Path to the repo being improved |
| `GPTME_DIR` | merge-reject, eval-score | Alias for ARTIFACT_DIR (backward compat) |
| `SUITE` | merge-reject, eval-score | Eval suite name |
| `MODEL` | eval-score | Model for eval (set by merge-reject loop) |
| `MODEL_CANDIDATES` | merge-reject | Comma-separated agent model list |
| `EVAL_MODEL_CANDIDATES` | merge-reject, eval-score | Comma-separated eval model list |
| `EVAL_CMD` | eval-score | Custom eval command |
| `AGENT_HARNESS` | merge-reject | Agent runner (`gptme` or `claude-code`) |
| `PROGRAM_SPEC` | merge-reject | Path to agent instruction file |
| `BRANCH` | merge-reject | Branch for commits (default: `autoresearch/eval-improvement`) |
| `MAX_ITERATIONS` | merge-reject | Max iterations per run |
| `PUBLISH_THRESHOLD` | merge-reject | Min score delta for auto-PR |
| `AUTORESEARCH_STATE_DIR` | check-status | State directory path |

## Examples

See `examples/` for:
- `gptme-eval.yaml` — Improve gptme's practical5 eval pass rate
- `gptme-eval-program.md` — Agent instruction file for gptme eval improvement
