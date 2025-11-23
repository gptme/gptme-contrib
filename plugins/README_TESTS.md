# Testing gptme Plugins

## Quick Start

```bash
# Run all fast tests (unit tests only)
uv run pytest plugins/ -m "not slow" --ignore=plugins/gptme_image_gen/tests/integration/

# Run per-plugin (recommended)
uv run pytest plugins/gptme_consortium/tests/ -v
uv run pytest plugins/gptme_image_gen/tests/ -v
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
uv run pytest plugins/gptme_consortium/tests/integration/ -v
uv run pytest plugins/gptme_image_gen/tests/integration/ -v

# Or with marker
uv run pytest plugins/ -m slow -v
```

## Test Structure

```txt
plugins/
├── gptme_consortium/
│   └── tests/
│       ├── conftest.py              # Test configuration
│       ├── test_consortium.py       # Unit tests (8 tests)
│       └── integration/
│           └── test_consortium_integration.py  # Integration tests (5 tests)
└── gptme_image_gen/
    └── tests/
        ├── conftest.py              # Test configuration
        ├── test_image_gen.py        # Unit tests (5 tests)
        └── integration/
            └── test_image_gen_integration.py  # Integration tests (8 tests)
```

## Known Issues

**Pytest discovery from root**: When running `pytest plugins/` from repository root, there may be import issues with integration tests. Workaround: Run per-plugin as shown above.

## CI Configuration

See task plan for GitHub Actions workflow configuration that:
- Runs fast tests on every PR
- Runs integration tests on push to main
- Uses repository secrets for API keys
