---
match:
  keywords: ["optimize", "performance", "caching", "speed up", "faster", "efficiency"]
status: active
---

# Simplify Before Optimize

## Rule
Always simplify a system BEFORE optimizing it. Optimizing something that shouldn't exist is waste.

## Context
Applies when considering performance improvements, caching, acceleration, or efficiency work.

## Detection
Observable signals:
- Jumping to caching without simplifying logic
- Optimizing complex workflows before questioning necessity
- Adding layers of optimization to already-complex systems
- "Just needs to be faster" without "needs to be simpler"

## Pattern
CORRECT ORDER (Elon's 5-Step Process):
1. Question requirements (does this need to exist?)
2. Delete unnecessary parts (remove before optimizing)
3. Simplify remaining parts (make it cleaner)
4. THEN accelerate (make it faster)
5. THEN automate (make it automatic)

EXAMPLE - Good sequence:
```python
# Context generation optimization

# Step 1: Question - do we need all this context?
# Result: Identified unnecessary data sources

# Step 2: Delete - remove unused sources
# Result: 3 sources → 2 sources

# Step 3: Simplify - consolidate logic
# Result: shell script → Python orchestrator

# Step 4: Accelerate - add caching
# Result: 30s → 5s (81.5% faster)

# Step 5: Automate - pre-commit hook
# Result: automatic validation
```

ANTI-PATTERN - Wrong sequence:
```python
# Building a complex system

# Jumped to Step 5: Build comprehensive system
adaptive_compression = ComplexSystem(
    task_analyzer,
    workspace_discovery,
    meta_learning,
    # 6000+ lines
)

# Skipped Steps 1-3:
# - Never questioned if simpler approach was sufficient
# - Never deleted unnecessary complexity
# - Never simplified before building

# Result: Overengineered, hard to maintain
```

RULE OF THUMB:
If you're adding caching/optimization to complex code:
1. STOP ⛔
2. Simplify the code first
3. Delete unnecessary parts
4. THEN add caching/optimization

## Outcome
Following this prevents:
- Optimizing wrong things (waste of effort)
- Maintaining complex optimizations (technical debt)
- Missing simple solutions (overengineering)

Following this enables:
- Clear understanding (simple systems easier to optimize)
- Better optimization (clear target after simplification)
- Maintainable code (simple + optimized)
