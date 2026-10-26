.PHONY: test typecheck

export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:  ## Run tests for this package
	uv run --extra test pytest tests/ -v

typecheck:  ## Run mypy for this package (strict, via [tool.mypy] in pyproject.toml)
	uv run --with mypy mypy src/
