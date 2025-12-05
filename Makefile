check-names:
	@# If we are in gptme-contrib, we should have no agent/instance/user-specific names, and vice versa
	@if [ "$(shell basename $(CURDIR))" = "gptme-contrib" ]; then \
		! git grep -i "bob\|alice\|erik@|@gmail" -- ':!Makefile' ':!fork.sh' ':!scripts/fork.py'; \
	fi
