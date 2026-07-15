.PHONY: test typecheck

MYPY_CACHE_DIR ?= $(shell git rev-parse --show-toplevel)/.mypy_cache

test:  ## Run tests for this package
	@if [ -d tests ] && [ "$$(find tests -name '*.py' 2>/dev/null | head -1)" ]; then \
		uv run --with pytest --with pytest-asyncio pytest tests/ -v; \
	else \
		echo "No tests found"; \
	fi

typecheck:  ## Run mypy for this package
	uv run --with mypy --with types-PyYAML --with tomli mypy -p lib --disable-error-code=unused-ignore
