.PHONY: test typecheck

test:  ## Run tests for this package
	@if [ -d tests ] && [ "$$(find tests -name '*.py' 2>/dev/null | head -1)" ]; then \
		uv run --with pytest pytest tests/ -v; \
	else \
		echo "No tests found"; \
	fi

typecheck:  ## Run mypy for this package
	uv run --with mypy --with types-PyYAML --with tomli mypy -p lib
