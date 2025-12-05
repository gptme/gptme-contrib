.PHONY: test typecheck

test:  ## Run tests for this package
	@if [ -d "tests" ] && [ -n "$$(ls -A tests/ 2>/dev/null | grep -E '\.py$$')" ]; then \
		uv run pytest tests/ -v; \
	else \
		echo "No tests found for lessons package"; \
	fi

typecheck:  ## Run mypy for this package
	uv run mypy -p lessons
