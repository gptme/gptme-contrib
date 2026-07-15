export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy --with types-PyYAML mypy src/ --ignore-missing-imports
