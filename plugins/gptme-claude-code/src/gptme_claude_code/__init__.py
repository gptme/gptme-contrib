"""
Claude Code plugin for gptme.

Spawn Claude Code subagents for various coding tasks:
- analyze: Code reviews, security audits, test coverage
- ask: Answer questions about codebases
- fix: Fix lint errors, build issues, type errors
- implement: Implement features (optionally in worktrees)
"""

__version__ = "0.1.0"

from .tools.claude_code import tool

__all__ = ["tool"]
