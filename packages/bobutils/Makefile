.PHONY: test typecheck

test:  ## Run tests for this package
	uv run pytest tests/ -v

typecheck:  ## Run mypy for this package
	uv run --with mypy mypy src/ --ignore-missing-imports
