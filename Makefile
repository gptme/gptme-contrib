.PHONY: test typecheck check-names help

test:  ## Run tests for all packages that have tests
	@echo "Running tests for packages..."
	@success=true; \
	for pkg_dir in packages/*/; do \
		pkg_name=$$(basename "$$pkg_dir"); \
		if [ -d "$$pkg_dir/tests" ] && [ -n "$$(ls -A $$pkg_dir/tests/ 2>/dev/null | grep -E '\.py$$')" ]; then \
			echo "\n=== Testing $$pkg_name ==="; \
			if [ -f "$$pkg_dir/Makefile" ] && grep -q "^test:" "$$pkg_dir/Makefile"; then \
				$(MAKE) -C "$$pkg_dir" test || success=false; \
			else \
				(cd "$$pkg_dir" && uv run pytest tests/ -v -m "not slow") || success=false; \
			fi; \
		fi; \
	done; \
	if [ "$$success" = "true" ]; then \
		echo "\n✓ All package tests passed"; \
	else \
		echo "\n✗ Some package tests failed"; \
		exit 1; \
	fi

typecheck:  ## Run typecheck for all packages that have a typecheck target
	@echo "Running typecheck for packages..."
	@success=true; \
	for pkg_dir in packages/*/; do \
		pkg_name=$$(basename "$$pkg_dir"); \
		if [ -f "$$pkg_dir/Makefile" ] && grep -q "^typecheck:" "$$pkg_dir/Makefile"; then \
			echo "\n=== Typechecking $$pkg_name ==="; \
			$(MAKE) -C "$$pkg_dir" typecheck || success=false; \
		fi; \
	done; \
	if [ "$$success" = "true" ]; then \
		echo "\n✓ All package typechecks passed"; \
	else \
		echo "\n✗ Some package typechecks failed"; \
		exit 1; \
	fi

check-names:  ## Check for agent-specific names (should be none in contrib)
	@# If we are in gptme-contrib, we should have no agent/instance/user-specific names, and vice versa
	@# Exclusions:
	@#   - Makefile, fork.sh, fork.py: forking scripts need agent names
	@#   - analyze-lesson-usage.py: needs configurable agent name list
	@if [ "$(shell basename $(CURDIR))" = "gptme-contrib" ]; then \
		! git grep -i "bob\|alice\|erik@|@gmail" -- ':!Makefile' ':!fork.sh' ':!scripts/fork.py' ':!packages/lessons/src/lessons/analyze-lesson-usage.py'; \
	fi

help:  ## Show this help message
	@echo "gptme-contrib Makefile targets:"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
