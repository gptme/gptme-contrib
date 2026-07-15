.PHONY: test typecheck

export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache

test:  ## Run tests for this package
	uv run --with pytest --with pytest-mock pytest tests/ -v

typecheck:  ## Run mypy for this package
	uv run --with mypy --with types-PyYAML mypy -p run_loops --disable-error-code=var-annotated --disable-error-code=no-any-return
