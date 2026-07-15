.PHONY: test typecheck

MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy --with types-PyYAML mypy -p gptme_daily_briefing --ignore-missing-imports
