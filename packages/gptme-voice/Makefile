export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:
	uv run --with pytest --with pytest-asyncio --package gptme-voice pytest tests/ -v

typecheck:
	-uv run --with mypy --package gptme-voice mypy src/ --ignore-missing-imports
