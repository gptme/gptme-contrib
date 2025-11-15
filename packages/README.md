# gptme-contrib Packages

This directory contains Python packages providing utilities and shared code for gptme agents.

## Package Structure

Each package follows the modern Python "src layout":

```text
package-name/
├── pyproject.toml          # Package configuration
├── README.md              # Package documentation
├── src/
│   └── package_name/      # Source code
│       ├── __init__.py
│       └── *.py
└── tests/                 # Package tests
    └── test_*.py
```

## Available Packages

### lib - Shared Library Code

**Location**: `packages/lib/`

Provides core utilities used across gptme agent systems:

- **Input Orchestrator**: Multi-source coordination (GitHub, Email, Webhooks, Scheduler)
- **Configuration Management**: Agent-specific settings and environment handling
- **Monitoring**: Logging and status tracking for autonomous operations
- **Rate Limiting**: API rate limit management

**Key Modules**:
- `lib.orchestrator`: Main orchestration service
- `lib.input_sources`: Abstract input source definitions
- `lib.input_source_impl`: Concrete implementations (GitHub, Email, etc.)
- `lib.config`: Configuration loading and validation
- `lib.rate_limiter`: Rate limit enforcement
- `lib.monitoring`: Logging and monitoring utilities

**Usage**:
```python
from lib.orchestrator import InputOrchestrator, OrchestratorConfig

config = OrchestratorConfig(...)
orchestrator = InputOrchestrator(config)
await orchestrator.run_once()
```

## Development

### Installing Packages

From workspace root:
```bash
# Install all packages
uv sync

# Install specific package in development mode
uv pip install -e packages/lib
```

### Running Tests

```bash
# Run tests for specific package
uv run pytest packages/lib/tests

# Run all tests
uv run pytest packages/*/tests
```

### Adding Dependencies

Add dependencies to the specific package's `pyproject.toml`:

```toml
[project]
dependencies = [
    "requests>=2.28",
]

[project.optional-dependencies]
test = [
    "pytest>=8.0",
]
```

Then sync the workspace:
```bash
uv sync
```

## Package Guidelines

1. **Minimal Dependencies**: Keep package dependencies lean
2. **Clear Purpose**: Each package should have a focused responsibility
3. **Documentation**: Include README and docstrings
4. **Testing**: Provide test coverage for core functionality
5. **Type Hints**: Use type annotations throughout

## Migration from Bob's Workspace

These packages were originally developed in Bob's workspace (`ErikBjare/bob`) and migrated to gptme-contrib to enable reuse across all gptme agents.

**Original Locations**:
- `lib` → from `bob/packages/lib/`

## Related

- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) - Template for creating new agents
- [gptme](https://github.com/gptme/gptme) - Core gptme framework
