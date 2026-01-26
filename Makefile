.PHONY: help test typecheck test-packages typecheck-packages test-plugins check-names list-packages list-plugins

# Dynamic discovery - find all directories with Makefile (skip symlinks)
PACKAGE_DIRS := $(shell find packages -maxdepth 1 -mindepth 1 -type d ! -type l ! -name '__pycache__' 2>/dev/null)
PLUGIN_DIRS := $(shell find plugins -maxdepth 1 -mindepth 1 -type d ! -type l 2>/dev/null)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

test: test-packages test-plugins test-integration  ## Run all tests

test-integration:  ## Run integration tests (git hooks, etc.)
	@echo "Running integration tests..."
	uv run --with pytest pytest tests/integration/ -v

typecheck: typecheck-packages  ## Run all type checks

# ============================================================
# Package targets (dynamically discovered)
# ============================================================

list-packages:  ## List all discovered packages
	@echo "Discovered packages:"
	@for pkg in $(PACKAGE_DIRS); do echo "  - $$(basename $$pkg)"; done

test-packages:  ## Run tests for all packages
	@echo "Running tests for all packages..."
	@failed=; \
	for pkg in $(PACKAGE_DIRS); do \
		if [ -f "$$pkg/Makefile" ]; then \
			echo "\n=== Testing $$(basename $$pkg) ==="; \
			$(MAKE) -C "$$pkg" test || failed=1; \
		fi \
	done; \
	if [ -n "$$failed" ]; then exit 1; fi

typecheck-packages:  ## Run mypy for all packages
	@echo "Running typecheck for all packages..."
	@failed=; \
	for pkg in $(PACKAGE_DIRS); do \
		if [ -f "$$pkg/Makefile" ]; then \
			echo "\n=== Typechecking $$(basename $$pkg) ==="; \
			$(MAKE) -C "$$pkg" typecheck || failed=1; \
		fi \
	done; \
	if [ -n "$$failed" ]; then exit 1; fi

# ============================================================
# Plugin targets (dynamically discovered)
# ============================================================

list-plugins:  ## List all discovered plugins
	@echo "Discovered plugins:"
	@for plugin in $(PLUGIN_DIRS); do echo "  - $$(basename $$plugin)"; done

test-plugins:  ## Run tests for all plugins with test directories
	@echo "Running tests for all plugins..."
	@failed=; \
	for plugin in $(PLUGIN_DIRS); do \
		if [ -d "$$plugin/tests" ]; then \
			echo "\n=== Testing $$(basename $$plugin) ==="; \
			uv run --with pytest pytest "$$plugin/tests" -v -m "not slow" --timeout=30 || failed=1; \
		fi \
	done; \
	if [ -n "$$failed" ]; then exit 1; fi

# ============================================================
# CI Helper targets (for GitHub Actions matrix)
# ============================================================

ci-list-packages-json:  ## Output packages as JSON array for CI matrix
	@for pkg in $(PACKAGE_DIRS); do if [ -f "$$pkg/Makefile" ]; then basename $$pkg; fi; done | jq -R -s -c 'split("\n") | map(select(length > 0))'

ci-list-plugins-json:  ## Output plugins with tests as JSON array for CI matrix
	@for plugin in $(PLUGIN_DIRS); do if [ -d "$$plugin/tests" ]; then basename $$plugin; fi; done | jq -R -s -c 'split("\n") | map(select(length > 0))'

check-names:  ## Validate naming patterns (no instance names in template)
	@bash scripts/precommit/check-names.sh
