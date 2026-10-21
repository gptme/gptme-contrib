.PHONY: test typecheck

PYTHON ?= $(shell command -v python3)
export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache

# Keep this package's Python >=3.10 tests independent of sibling workspace floors.
test:
	uv run --isolated --no-project --python "$(PYTHON)" --with '.[test]' pytest tests/ -v

typecheck:
	uv run --isolated --no-project --python "$(PYTHON)" --with '.[dev]' mypy src/
