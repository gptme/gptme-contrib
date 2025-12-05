check-names:
	@# If we are in gptme-contrib, we should have no agent/instance/user-specific names, and vice versa
	@# Exclusions:
	@#   - Makefile, fork.sh, fork.py: forking scripts need agent names
	@#   - analyze-lesson-usage.py: needs configurable agent name list
	@if [ "$(shell basename $(CURDIR))" = "gptme-contrib" ]; then \
		! git grep -i "bob\|alice\|erik@|@gmail" -- ':!Makefile' ':!fork.sh' ':!scripts/fork.py' ':!packages/lessons/src/lessons/analyze-lesson-usage.py'; \
	fi
