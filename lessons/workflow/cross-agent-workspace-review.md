---
match:
  keywords: ["review", "workspace", "cross-agent", "bob", "collaboration", "patterns"]
status: active
---

# Cross-Agent Workspace Review

## Rule
Conduct mutual workspace reviews between agents to identify improvement opportunities and share proven patterns.

## Context
When agents with different specializations (technical vs strategic, infrastructure vs domain-specific) can learn from each other's approaches.

## Detection
Observable signals for valuable cross-agent review:
- Another agent has demonstrated superior patterns in their domain
- Your workspace has grown organically without systematic review
- Opportunity for knowledge sharing between complementary agents
- Need to identify upstream contribution opportunities

## Pattern
Structured mutual review process:
```shell
# 1. Request access to other agent's workspace
# (Erik can grant cross-repo access)

# 2. Systematic workspace analysis
# Focus areas:
# - Configuration and automation patterns
# - Knowledge organization strategies
# - Lesson architecture and categorization
# - Tool and script ecosystems
# - Context efficiency approaches

# 3. Document findings in structured format:
# - What's exceptional (patterns worth adopting)
# - Areas for improvement (specific suggestions)
# - Upstream contribution opportunities
# - Implementation priorities

# 4. Share findings via GitHub issues for discussion
# 5. Implement agreed improvements through proper PR process
```

## Specific Review Areas

**Configuration Analysis:**
- `gptme.toml` sophistication and pattern usage
- Context loading strategies (static vs dynamic)
- Lesson system organization and keyword matching

**Knowledge Organization:**
- Directory structure and categorization
- Documentation patterns and accessibility
- Cross-referencing and navigation approaches

**Infrastructure Maturity:**
- Script ecosystems and automation level
- Health monitoring and status tracking
- Tool integration and workflow optimization

## Outcome
Following this pattern results in:
- **Cross-pollination**: Best practices spread between agents
- **Rapid improvement**: Learn from proven patterns instead of reinventing
- **Upstream contributions**: Identify shared utilities for gptme-contrib
- **Specialization benefits**: Leverage different agents' domain expertise
- **Quality improvements**: External perspective identifies blind spots

## Example Success Metrics
From Bob-Alice collaboration (Issue #6):
- Alice improved context efficiency with dynamic loading patterns
- Bob identified new Darwinian trap patterns from Alice's work
- Both agents identified upstream contribution opportunities
- Infrastructure improvements validated through real-world usage

## Related
- [Inter-Agent Communication](./inter-agent-communication.md) - Communication protocols
- [Git Workflow](./git-workflow.md) - PR and review processes

## Origin
2025-12-11: Extracted from successful Bob-Alice workspace review collaboration in Issue #6, demonstrating value of systematic cross-agent knowledge sharing.
