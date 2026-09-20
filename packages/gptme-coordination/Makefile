export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:
	uv run --with pytest --package gptme-coordination pytest tests/ -v

typecheck:
	uv run --with mypy --package gptme-coordination mypy src/ --ignore-missing-imports
