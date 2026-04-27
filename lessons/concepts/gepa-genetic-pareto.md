---
match:
  keywords:
    - "gepa-lesson-optimizer"
status: active
---

# GEPA-Based Lesson Optimization

## Rule
Use GEPA-inspired mutation to fix underperforming lessons: diagnose first, mutate content/keywords, validate with LLM-as-judge before applying.

## Context
When LOO analysis shows a lesson with statistically significant negative delta (Δ < -0.05, p < 0.05) and the lesson content or keywords appear to be the cause.

## Detection
- `lesson-loo-analysis.py` shows lesson with negative Δ and sufficient data (n ≥ 15)
- Lesson fires on sessions where it's irrelevant (broad keywords)
- Lesson provides concept definition instead of behavioral guidance

## Pattern
```bash
# 1. Diagnose underperformers (agent-workspace script)
python3 scripts/gepa-lesson-optimizer.py --bottom 5 --diagnose

# 2. Run Sonnet mutations — generates candidates, does NOT apply yet
python3 scripts/gepa-lesson-optimizer.py --bottom 5 --mutate

# 3. Validate with LLM-as-judge BEFORE applying
python3 scripts/gepa-lesson-optimizer.py --bottom 5 --judge

# 4. Apply only after judge passes
python3 scripts/gepa-lesson-optimizer.py --bottom 5 --apply
```

## Common fixes
- **Broad keywords** → narrow to specific trigger phrases (multi-word, not single words)
- **Concept definition** → replace with behavioral rule + pattern
- **Already in GLOSSARY** → archive; glossary covers the concept

## Outcome
- Lesson no longer fires on irrelevant sessions
- LOO delta improves over next 2-week validation window
- Bottom-N pool shrinks toward noise floor

## Related
- Agent-workspace: `scripts/gepa-lesson-optimizer.py` — mutation + judge tooling
- Agent-workspace: `scripts/lesson-loo-analysis.py` — effectiveness signal
- `knowledge/lessons/` — companion docs with full detail
