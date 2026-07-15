MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy --with types-PyYAML mypy src/ --ignore-missing-imports
