# LOO Lesson Evaluation: Confounds and Better Alternatives

**Context**: When agents use Thompson sampling bandits to track which lessons improve session outcomes, Leave-One-Out (LOO) analysis is a tempting evaluation signal. But it has structural confounds that make it unreliable as an optimization target. This doc explains what they are and what to use instead.

## What LOO Measures

LOO lesson evaluation compares session outcomes in two groups:
- Sessions **with** the lesson injected
- Sessions **without** the lesson (approximated by sessions where it didn't match)

A positive LOO score means "sessions where this lesson was present tended to be more productive." But that correlation doesn't mean the lesson caused the productivity.

## The Three Structural Confounds

### 1. Session-Selection Confound

**Problem**: Lessons about failure patterns only match sessions that are already in trouble.

A lesson like "When git push fails, check remote tracking branch" only triggers when the agent encounters push failures. These sessions are already harder and more prone to failure. LOO compares these hard sessions against easy sessions where the lesson never matched — making the lesson look *harmful* even if it resolved the issue.

**Signal direction**: Lessons targeting known failure modes will show artificially low LOO scores, potentially getting archived for the wrong reason.

**Test**: If a lesson has a clearly-described fix for a real problem, but shows negative LOO score — check whether it only matches in sessions with pre-existing failures.

### 2. Category Baseline Confound

**Problem**: "Strategic" sessions have naturally higher productivity baselines than "code" sessions or "infrastructure" sessions.

If a lesson with broad keywords (e.g., "task management") happens to match more often in strategic sessions, it accumulates rewards from that category's baseline — not from its actual impact. Conversely, a lesson that primarily helps in debugging sessions gets penalized by that category's lower baseline.

**Signal direction**: Lessons in high-baseline categories get inflated LOO scores; lessons in low-baseline categories get deflated scores. Category diversity in lesson matches ≠ category-corrected signal.

**Fix**: Use per-category residual reward (`reward - category_baseline`) when updating bandits. Requires tracking `session_category` alongside rewards.

### 3. Broad-Keyword Confound

**Problem**: Lessons with generic keywords (e.g., matching "git", "commit", "test") appear in nearly every session, making LOO estimation unreliable.

LOO works by finding sessions *without* the lesson as a control group. When a lesson matches 90% of all sessions, the "without" group is a tiny, unrepresentative sample. The LOO estimate is noisy and biased toward recent outliers.

**Signal direction**: Unstable LOO scores with high variance. The lesson may score differently across time windows purely from noise.

**Fix**: Use multi-word, specific trigger phrases as keywords. See `lessons/README.md` keyword selection guidance.

## Practical Implications

### LOO is a useful LAGGING ARCHIVE signal

LOO is good for one thing: catching lessons that are *clearly harmful* over many sessions. A lesson with consistently negative LOO across 50+ sessions, across multiple categories, is likely a bad lesson.

Use LOO for:
- ✅ **Archiving** underperforming lessons (requires many sessions, not few)
- ✅ **Monitoring** lesson quality trends over weeks
- ✅ **Identifying** candidate lessons for manual review

### LOO is a bad OPTIMIZATION signal

LOO is too slow and too confounded for:
- ❌ **GEPA iteration** — needs hundreds of rollouts for reliable gradient; LOO needs weeks
- ❌ **Fast A/B testing** — confounds dominate at low sample counts
- ❌ **Direct bandit optimization** — category and selection confounds flip signal direction

## Better Alternatives

### For fast lesson optimization (GEPA-style)

**LLM-as-judge over matched trajectories** — given a session trajectory where the lesson matched, ask an LLM: "Did the agent follow this lesson's guidance? Did it prevent the known failure mode?" This generates a per-lesson causal signal without requiring a control group.

Store judge results as session record annotations (cacheable: judge once, replay for free). Pass the trajectories to the *mutation step* so the rewriting agent reasons directly about whether the lesson helped.

### For causal measurement at scale

**Randomized lesson injection** — make lesson injection stochastic: when a lesson matches, inject with probability `bandit_arm.sample()` instead of always. The bandit arm IS the experiment. LOO confounding disappears because assignment is now random.

This requires harness-level support but is the most statistically sound approach. The bandit's exploration automatically creates the control group.

### For category baseline normalization

**Residual reward** — instead of raw `1.0` (productive) / `0.0` (noop), use:

```python
reward = outcome_reward - category_baseline[session_category]
```

Where `category_baseline` is the historical productive-session rate for that category. This corrects for the category confound without randomization.

Requires 2-4 weeks of `session_category`-tagged sessions to calibrate baselines before enabling.

### For lagging archive decisions

**Keep LOO**, but:
- Use category-controlled LOO (compare within category, not across)
- Require minimum sample count (50+ matched sessions) before acting
- Treat it as "evidence to trigger manual review", not automated archiving

## Summary

| Use case | LOO | Better alternative |
|----------|-----|--------------------|
| Archive clearly bad lessons | ✅ (with caveats) | Category-controlled LOO + sample threshold |
| Fast optimization (GEPA) | ❌ | LLM-as-judge over matched trajectories |
| Causal A/B measurement | ❌ | Randomized injection |
| Category bias correction | ❌ | Residual reward normalization |
| Trend monitoring | ✅ | Rolling LOO with category stratification |

## Related

- `lessons/README.md` — lesson keyword selection best practices
- [Thompson Sampling for Work Categories](../../../lessons/autonomous/thompson-sampling-work-categories.md) — category selection bandit that uses the same session-grade reward signal LOO analyzes (note: this is a work-category bandit, not a lesson bandit)
- `packages/gptme-sessions/` — `SessionRecord.recommended_category` field for per-category tracking
- [GEPA (Genetic-Pareto)](../../../lessons/concepts/gepa-genetic-pareto.md) — lesson optimization that needs a better-than-LOO eval signal
