.PHONY: test typecheck

test:  ## Run tests for this package
	uv run pytest tests/ -v -m "not slow"

typecheck:  ## Run mypy for this package
	uv run --with mypy mypy -p run_loops
