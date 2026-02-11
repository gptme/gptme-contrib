.PHONY: install test typecheck format publish

install:  ## Install the package in development mode
	uv pip install -e .

test:  ## Run tests for this package
	uv run --with pytest --with pytest-cov pytest tests/ -v

typecheck:  ## Run mypy for this package (relaxed for initial upstream)
	uv run --with mypy --with types-requests --with types-Markdown mypy -p gptmail \
		--ignore-missing-imports \
		--disable-error-code=var-annotated \
		--disable-error-code=no-untyped-def \
		--disable-error-code=return-value \
		--disable-error-code=attr-defined

format:  ## Format code
	uv run --with ruff ruff format src/ tests/
	uv run --with ruff ruff check --fix src/ tests/

publish:  ## Build and publish to PyPI
	uv build
	uv publish
