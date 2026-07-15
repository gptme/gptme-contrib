.PHONY: test typecheck publish

export MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache
export RUFF_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.ruff_cache
export PYTEST_ADDOPTS ?= --cache-dir=$(shell git rev-parse --show-toplevel)/.pytest_cache

test:  ## Run tests for this package
	@if [ -d tests ] && [ "$$(find tests -name '*.py' 2>/dev/null | head -1)" ]; then \
		uv run --with pytest pytest tests/ -v; \
	else \
		echo "No tests found"; \
	fi

typecheck:  ## Run mypy for this package
	uv run --with mypy --with types-tabulate --with types-PyYAML mypy -p gptodo --ignore-missing-imports --disable-error-code=unused-ignore

publish:  ## Build and publish to PyPI
	uv build
	uv publish
