export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:
	uv run --extra test pytest tests/ -v -m "not slow"

test-all:
	uv run --extra test pytest tests/ -v

typecheck:
	uv run --extra dev mypy src/ --ignore-missing-imports
