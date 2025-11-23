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

### GitHub Actions Workflow

The repository includes `.github/workflows/test-plugins.yml` that:
- Runs unit tests on every push and PR (Python 3.10, 3.11, 3.12)
- Runs integration tests on push to master (when API keys available)
- Generates coverage reports and uploads to Codecov
- Timeout: 30s for unit tests, 120s for integration tests

### Required GitHub Secrets

Configure these secrets in repository settings for integration tests:

- `ANTHROPIC_API_KEY` - For Claude models in consortium
- `OPENAI_API_KEY` - For GPT models in consortium and DALL-E
- `GOOGLE_API_KEY` - For Gemini models in image generation

**Setup**: Repository Settings → Secrets and variables → Actions → New repository secret

### Local Testing Before CI

Test the same way CI will run:

```bash
# Unit tests (fast, no secrets needed)
pytest plugins/*/tests/ -v -m "not slow and not integration" --timeout=30

# Integration tests (requires API keys)
export ANTHROPIC_API_KEY="your-key"
export OPENAI_API_KEY="your-key"
export GOOGLE_API_KEY="your-key"
pytest plugins/*/tests/ -v -m "slow or integration" --timeout=120

# Coverage report
pytest plugins/*/tests/ -v -m "not slow and not integration" \
  --cov=plugins --cov-report=xml --cov-report=term
```

### Testing Workflow Changes

To test workflow changes without push to master:
1. Create feature branch
2. Push branch to trigger PR workflow
3. Only unit tests run on PR (integration tests require master push)

## Phase 4: Feature Tests (COMPLETE)

### Overview
Simplified feature tests focusing on configuration, error handling, edge cases, and data structures.
These tests verify plugin interfaces and behavior without requiring complex API mocking.

### Coverage
- **Error Handling**: Invalid inputs, missing dependencies, API key validation
- **Configuration**: Parameter validation, default values, option acceptance
- **Edge Cases**: Long inputs, unicode, special characters, empty values
- **Data Structures**: Dataclass definitions, field types, proper structure
- **Integration**: ToolSpec configuration, function callability, block types

### Test Counts
- **image_gen**: 12 tests
  - Error handling: 3 tests
  - Configuration: 2 tests
  - Edge cases: 3 tests
  - Provider options: 2 tests
  - Data structures: 2 tests

- **consortium**: 16 tests
  - Configuration: 4 tests
  - Edge cases: 4 tests
  - Data structures: 2 tests
  - Provider options: 2 tests
  - Integration: 4 tests

### Running Phase 4 Tests
```bash
# Run all Phase 4 tests
uv run pytest plugins/gptme_image_gen/tests/feature/test_features_simple.py
uv run pytest plugins/gptme_consortium/tests/feature/test_features_simple.py

# Run specific test class
uv run pytest plugins/gptme_image_gen/tests/feature/test_features_simple.py::TestErrorHandling -v
```

### Design Philosophy
Phase 4 tests are intentionally simplified compared to the initial comprehensive feature test attempt:
- Focus on what's testable without complex mocking
- Complement Phase 1 (unit) and Phase 2 (integration) tests
- Verify plugin interfaces and configuration handling
- Ensure robust error handling and edge case coverage
- Fast execution (< 2 seconds total)

This pragmatic approach provides valuable coverage without the maintenance burden of extensive mock-based tests.
