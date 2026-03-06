---
status: active
category: autonomous
tags:
- thompson-sampling
- work-category
- session-optimization
- cascade
- bandit
match:
  keywords:
  - work category bandit
  - Thompson sampling categories
  - what kind of work should I do
  - cascade category
  - category optimization
  - category bandit
  - work category optimization
---

# Thompson Sampling for Work Category Optimization

## Rule
Use a `Bandit` from `gptme_sessions.thompson_sampling` to learn which *work categories* are most productive for your agent, not which *session modes* to run. Categories (code, triage, infrastructure, content) are the explore/exploit decision; session pipelines are a scheduling choice.

## Context
When building autonomous agent sessions that span multiple types of work — code fixes, issue triage, infrastructure improvement, content creation, etc. — the question "what should I work on today?" is an explore/exploit problem that Thompson sampling can answer using accumulated session data.

This pattern is the complement to [rule-driven session gating](./rule-driven-session-gating.md): gating decides *whether* to run; category selection decides *what kind of work* to do when running.

## Detection
Observable signals indicating this pattern applies:
- Agent does multiple types of work across sessions (not just one category)
- Category selection is round-robin, random, or purely gut-based
- You already record sessions with `gptme-sessions` and have outcome data
- Some categories feel more productive but you're not sure why or how often

## Pattern

### 1. Set up the category bandit

```python
from gptme_sessions import Bandit

CATEGORIES = ["code", "triage", "infrastructure", "content", "cross-repo", "self-review"]

bandit = Bandit(state_dir="state/category-bandit")
scores = bandit.sample(CATEGORIES)
recommended = max(scores, key=scores.get)
print(f"Recommended: {recommended} (score: {scores[recommended]:.3f})")
```

### 2. Integrate posteriors into an existing scorer

If you have a base scoring system (like CASCADE), add an additive boost:

```python
from gptme_sessions import load_bandit_means

means = load_bandit_means("state/category-bandit", arm_ids=CATEGORIES)

def apply_ts_boost(base_score: float, category: str, weight: float = 1.5) -> float:
    """Additive boost: +weight at mean=1.0, 0 at mean=0.5, -weight at mean=0.0."""
    return base_score + weight * (means.get(category, 0.5) - 0.5)

# Example: score options with Thompson posterior boost
options = [
    {"name": "Fix flaky tests", "category": "code", "base_score": 5},
    {"name": "Review open issues", "category": "triage", "base_score": 4},
    {"name": "Update CI config", "category": "infrastructure", "base_score": 4},
]
for opt in options:
    opt["final_score"] = apply_ts_boost(opt["base_score"], opt["category"])
best = max(options, key=lambda x: x["final_score"])
```

### 3. Update the bandit after each session

Use `post_session()` — its returned `grade` is the ideal signal for bandit updates:

```python
from gptme_sessions import SessionStore, post_session, Bandit
from pathlib import Path

store = SessionStore(Path("state/sessions"))
bandit = Bandit(state_dir="state/category-bandit")

result = post_session(
    store=store,
    harness="claude-code",
    model="claude-opus-4-6",
    trigger="timer",
    category=selected_category,
    exit_code=exit_code,
    duration_seconds=duration,
    trajectory_path=trajectory_path,
    start_commit=start_sha,
    end_commit=end_sha,
)

# Update bandit with graded signal (0.0–1.0)
reward = result.grade if result.grade is not None else (1.0 if result.record.outcome == "productive" else 0.0)
bandit.update([selected_category], outcome=reward)
```

### 4. Add context for model-specific learning (optional)

If you run different models, use contextual arms to learn per-model category effectiveness:

```python
# Sample with context — use only the model name, NOT the category being selected
scores = bandit.sample(CATEGORIES, context=("opus",))

# Update with context — selected_category is known here, safe to include
bandit.update([selected_category], outcome=reward, context=("opus",))
```

### 5. View the current posterior

```bash
python3 -m gptme_sessions.thompson_sampling status --state-dir state/category-bandit
python3 -m gptme_sessions.thompson_sampling dashboard --state-dir state/category-bandit
```

## Anti-Pattern: Optimizing Session Modes

**Wrong** — using Thompson sampling to pick between `autonomous`, `monitoring`, `email` modes:

```python
# DON'T: These modes have obvious, structurally different success rates.
# 'autonomous' will always dominate because it does the most work.
# The bandit learns nothing useful.
scores = bandit.sample(["autonomous", "monitoring", "email"])
```

**Why this fails**: Mode posteriors measure "is this pipeline type productive?" — a question with an obvious answer (the mode that does the most work wins). The bandit's signal is noise, not insight.

**Correct** — Thompson sampling over work categories *within* a mode:

```python
# DO: Within an autonomous session, which category to focus on?
scores = bandit.sample(["code", "triage", "infrastructure", "content"])
```

The trigger/mode is a scheduling decision (handled by systemd timers, backoff, etc.). The category is the explore/exploit decision Thompson sampling is designed for.

## Outcome
Following this pattern results in:
- **Data-driven category selection**: Agent naturally gravitates toward productive categories
- **Exploration**: New or underexplored categories still get tried (Beta distribution uncertainty)
- **Adaptation**: Decay (via `Bandit.decay()`) handles non-stationarity as agent capabilities change
- **Transparency**: `dashboard` shows posterior means so you can see what the agent has learned

## Related
- [Rule-Driven Session Gating](./rule-driven-session-gating.md) — Complementary pattern (when to run)
- `gptme_sessions.thompson_sampling` — Thompson sampling engine (BanditArm, BanditState, Bandit)
- `gptme_sessions.post_session` — Post-session hook that provides the grade signal
- `gptme_sessions.load_bandit_means` — Read-only posterior integration for existing scorers

## Origin
2026-03-06: Extracted from the implementation of CASCADE + Thompson sampling in Bob's autonomous run system
(gptme/gptme-contrib#349). After upstreaming the bandit engine and post_session() hook to gptme-sessions,
this lesson documents the reusable pattern for any agent wanting to optimize work category selection.
The key insight — optimize *categories*, not *modes* — came from analyzing why the run-type bandit
had posteriors that confirmed the obvious (autonomous sessions are productive) rather than the actionable.
