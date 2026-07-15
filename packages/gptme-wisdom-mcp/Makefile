export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache
export PYTEST_ADDOPTS ?= --cache-dir=$(shell git rev-parse --show-toplevel)/.pytest_cache

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy mypy src/ --ignore-missing-imports
