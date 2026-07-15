.PHONY: test typecheck

MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache

test:  ## Run tests for this package
	uv run pytest tests/ -v

typecheck:  ## Run mypy for this package
	uv run --with mypy --with types-PyYAML mypy src/ --ignore-missing-imports
