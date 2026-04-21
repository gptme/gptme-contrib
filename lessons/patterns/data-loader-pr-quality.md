---
match:
  keywords:
  - data loader
  - csv loader
  - file not found
  - assert path
  - missing file test
  - loader test
  - timezone assertion
  - unit validation
  session_categories: [code]
status: active
---

# Data Loader PR Quality Patterns

## Rule
When writing data loader PRs for any data pipeline, apply these six patterns to produce
production-quality code that passes automated review on the first cycle.

## Context
When submitting a PR that adds a new data loader: reading CSV exports, parsing SQLite
databases, querying APIs, or any function that ingests external data into a DataFrame.

## Detection
Observable signals that you need to apply these patterns:
- Writing a function that reads a file path argument
- Using `assert path.exists()` to validate input files
- Normalizing column names with `.str.lower()` + `.replace()` chains
- Loader module has multiple load functions but tests only cover one
- Checking `df.index.tz is not None` in tests
- Handling a `unit` or `format` string parameter with if/else
- Deciding whether to build a per-device parser or a hub integration

## Pattern

### 1. Error handling: FileNotFoundError over assert

```python
# Wrong: assert is stripped with python -O
assert path.exists(), f"File not found: {path}"

# Correct: explicit, always runs
if not path.exists():
    raise FileNotFoundError(f"File not found: {path}")
```

**Why**: `assert` statements are stripped when Python runs in optimized mode (`-O` flag).
Use explicit `if not ...: raise` for guards that must always be enforced.

### 2. No no-op string operations

```python
# Wrong: identity replacements have no effect
df.columns = (
    df.columns.str.lower()
    .str.replace(" ", "_")
    .str.replace("energy_kcal", "energy_kcal")  # no-op
    .str.replace("protein_g", "protein_g")      # no-op
)

# Correct: keep only transformations that actually change something
df.columns = df.columns.str.lower().str.replace(" ", "_").str.replace("(", "").str.replace(")", "")
```

Before committing: verify each replace/transform actually changes at least one value
in the real data.

### 3. Test every loader function

If your module has `load_nutrition_df` AND `load_servings_df`, both need tests.
The primary function gets tested; the secondary one is often forgotten.

Checklist for **each** loader function:
- `test_load_<function>` — happy path with a minimal fixture file
- `test_load_<function>_missing_file` — verifies `FileNotFoundError` when path is absent

### 4. Timezone assertions: be strict

```python
# Weak: passes if tz is set to anything
assert df.index.tz is not None

# Strong: verifies the actual timezone
assert str(df.index.tz) == "UTC"
```

Use `str(df.index.tz) == "UTC"` (works across pandas versions and pytz/zoneinfo).

### 5. Enum-like parameters: explicit allowlist + ValueError

```python
# Wrong: silently falls through to one branch for unknown inputs
if unit == "mg/dL":
    factor = 18.0
else:  # treats anything unknown as mmol/L — silent failure
    factor = 1.0

# Correct: raise on unsupported values
VALID_UNITS = {"mmol/L", "mg/dL"}
if unit not in VALID_UNITS:
    raise ValueError(f"Unsupported unit: {unit!r}. Expected one of {sorted(VALID_UNITS)}")
```

### 6. Hub vs. per-device architecture

Before building a per-device data loader, ask: does the user route this data through
a hub?

| Data source | Common hub |
|-------------|------------|
| Home sensors (temperature, CO2) | Home Assistant |
| Wearable fitness data | Apple Health, Google Fit, Garmin Connect |
| Smart home devices | Home Assistant, SmartThings |
| Financial accounts | Plaid, bank export |

A single hub loader covers all current and future devices automatically.
Per-device CSV parsers are only appropriate when there is no hub integration.

**Anti-pattern**: Building a per-device parser when a hub loader already exists or
could cover the same data. One hub loader > N per-device parsers.

## Review Checklist (before opening PR)

- [ ] `assert path.exists()` → `if not path.exists(): raise FileNotFoundError(...)`
- [ ] No no-op string operations (each transform changes at least one value)
- [ ] All loader functions have tests: happy path + missing-file error
- [ ] Timezone assertions use `str(df.index.tz) == "UTC"`, not just `is not None`
- [ ] Enum-like parameters validated with explicit allowlist + `ValueError`
- [ ] Checked whether data flows through a hub before building per-device parser

## Outcome
Following these patterns results in:
- **First-cycle review pass**: Automated reviewers (Sourcery, Greptile) don't flag
  these issues, reducing round-trips
- **Reliable error messages**: `FileNotFoundError` survives `-O` and gives useful context
- **Complete test coverage**: No loader path silently goes untested
- **Correct timezone handling**: UTC assertions catch tz-aware vs. tz-naive bugs early
- **Fail-fast validation**: Invalid input raises immediately with a clear message

## Related
- [Avoid Long Try Blocks](./avoid-long-try-blocks.md) - Keep exception scope tight
- [Simplify Before Optimize](./simplify-before-optimize.md) - Remove no-ops before adding features

## Origin
2026-03-06: Extracted from Sourcery AI review feedback across three data loader PRs
(nutrition, glucose CGM, environmental sensors). All six code patterns appeared in at
least two of the three PRs, indicating they are systematic gaps rather than one-offs.
The hub vs. per-device pattern emerged from a closed PR where a per-device parser was
superseded by an existing hub integration.
