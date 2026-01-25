# Testing gptme Plugins

## Quick Start

```bash
# Run all fast tests (unit tests only)
uv run pytest plugins/ -m "not slow" --ignore=plugins/gptme-imagen/tests/integration/

# Run per-plugin (recommended)
uv run pytest plugins/gptme-consortium/tests/ -v
uv run pytest plugins/gptme-imagen/tests/ -v
uv run pytest plugins/gptme-attention-tracker/tests/ -v
```

## Test Types

### Unit Tests (Fast)
- Mock external dependencies
- Run on every commit
- No API keys required

### Integration Tests (Slow)
- Use real APIs
- Require API keys: `GOOGLE_API_KEY`, `OPENAI_API_KEY`
- Marked with `@pytest.mark.slow`
- Skipped automatically if API keys not set

## Running Integration Tests

Set API keys and run:

```bash
export GOOGLE_API_KEY="your-key"
export OPENAI_API_KEY="your-key"

# Per plugin
uv run pytest plugins/gptme-consortium/tests/integration/ -v
uv run pytest plugins/gptme-imagen/tests/integration/ -v

# Or with marker
uv run pytest plugins/ -m slow -v
```

## Test Structure

```txt
plugins/
├── gptme-attention-tracker/
│   └── tests/
│       ├── test_attention_history.py  # History tracking tests
│       └── test_attention_router.py   # Router tests
├── gptme-consortium/
│   └── tests/
│       ├── conftest.py                # Test configuration
│       ├── test_consortium.py         # Unit tests (8 tests)
│       └── integration/
│           └── test_consortium_integration.py  # Integration tests (5 tests)
└── gptme-imagen/
    └── tests/
        ├── conftest.py                # Test configuration
        ├── test_image_gen.py          # Unit tests (5 tests)
        └── integration/
            └── test_image_gen_integration.py  # Integration tests (8 tests)
```

## Plugin Test Commands

| Plugin | Command |
|--------|---------|
| gptme-attention-tracker | `uv run pytest plugins/gptme-attention-tracker/tests/ -v` |
| gptme-claude-code | `uv run pytest plugins/gptme-claude-code/tests/ -v` |
| gptme-consortium | `uv run pytest plugins/gptme-consortium/tests/ -v` |
| gptme-gupp | `uv run pytest plugins/gptme-gupp/tests/ -v` |
| gptme-imagen | `uv run pytest plugins/gptme-imagen/tests/ -v` |
| gptme-lsp | `uv run pytest plugins/gptme-lsp/tests/ -v` |
| gptme-warpgrep | `uv run pytest plugins/gptme-warpgrep/tests/ -v` |
| gptme-wrapped | `uv run pytest plugins/gptme-wrapped/tests/ -v` |
