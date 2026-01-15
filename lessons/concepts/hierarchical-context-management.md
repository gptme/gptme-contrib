---
match:
  keywords:
  - hierarchical context management
  - Droid context architecture
  - agent context layers
  - Factory Droid context
  - multi-layer context
  - context layer architecture
  - agentic context layers
---

# Hierarchical Context Management (Droid Architecture)

> **Not to be confused with**: The "CASCADE" workflow used in gptme autonomous sessions, which refers to task selection priority (PRIMARY → SECONDARY → TERTIARY). This lesson is about context management architecture, not task prioritization.

## Term
**Hierarchical Context Management**: Context architecture pattern used in advanced agent systems like Factory's Droid.

## Definition
Multi-layered context management approach that organizes information in levels of importance and accessibility, assembling context per-step based on the current action.

## Architecture Layers

Common pattern in sophisticated agent systems:

- **Global Context**: System policies, guardrails, organization conventions, role definitions
- **Project/Repo Context**: Structural view of codebase, framework conventions, key entrypoints (retrieved on-demand via tools)
- **Session/Task Context**: Original request, evolving task state, decomposition artifacts from plan mode
- **Plan/Subtask Context**: Structured plan with steps, dependencies, required files - only active step injected
- **Action-Level Context**: Concrete files, test outputs, logs, diffs needed for next edit/command + local scratchpad
- **Tool/Environment Layer**: Tools via MCP, file system, git, test runner - results selectively injected

## Key Principles

1. **Step-wise assembly**: Context built per-step based on current action (planning, editing, testing, etc.)
2. **Aggressive pruning**: Summarize older reasoning, limit file content to relevant slices, drop completed branches
3. **Role-aware prompts**: Each specialized agent has own context configuration
4. **Guardrails by default**: Constraints in global and role layers enforced via prompt + tool policy

## Applicability

This pattern is valuable when:
- Managing complex multi-step tasks with varying context needs
- Building specialized agents with different context requirements
- Implementing context-efficient systems that avoid token waste

## Source
Pattern derived from Factory AI's Droid product and related agentic context engineering approaches. See also ACE (Agentic Context Engineering) for broader framework.

## Related
- ACE (Agentic Context Engineering)
- Context compression strategies
- Memory hierarchy patterns
