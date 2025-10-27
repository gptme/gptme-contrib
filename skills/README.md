# Skills

Skills are complete capability packages that bundle prompts with executable tools for specific domains.

## What are Skills?

**Skills** provide domain-specific capabilities through:
- Comprehensive prompts (SKILL.md) explaining the domain and available operations
- Executable tools (scripts/) for the LLM to use
- Reference documentation and examples

## Skills vs Lessons

**Skills** (this directory):
- Complete capability packages (200-500+ lines)
- Include executable tools and scripts
- Provide domain expertise (data analysis, web scraping)
- Examples with workflows and patterns

**Lessons** (`/home/bob/gptme-bob/lessons/`):
- Concise behavioral guidance (30-50 lines)
- Reference existing tools, don't include new ones
- Prevent known failures and mistakes
- Quick runtime decision support

## Structure

Each skill is a directory containing:
skill-name/
├── SKILL.md           # Main skill prompt (what the LLM reads)
├── scripts/           # Executable tools
│   ├── tool1.py
│   └── tool2.py
├── examples/          # Usage examples
│   └── example1.md
└── reference/         # Additional documentation
    └── guide.md

## Skill Metadata

Skills use YAML frontmatter in SKILL.md:

```yaml
---
name: skill-name
keywords: [keyword1, keyword2]
description: Brief description
tools: [tool1, tool2]
---
```

## How Skills Work

1. **Auto-inclusion**: Skills are automatically included in context when their keywords match the conversation
2. **Tool Discovery**: Tools in scripts/ are made available to the LLM
3. **Context-aware**: Only relevant skills loaded to preserve token budget

## Creating a New Skill

1. Create directory: `skills/your-skill/`
2. Write SKILL.md with comprehensive prompts
3. Add tools to scripts/ directory
4. Provide examples in examples/
5. Test with various prompts

## Available Skills

- None yet (coming in Phase 4.2)

## Integration with Lessons

Skills complement lessons:
- **Lessons**: Guide behavior ("when to research", "how to debug")
- **Skills**: Provide capabilities ("analyze data", "scrape web")

Both use keyword-based auto-inclusion, but skills are larger capability packages.

## Future Development

**Phase 4.2**: Port data-analysis skill
**Phase 4.3**: Port web-scraping skill
**Phase 5**: Cursor rules compatibility
**Phase 6**: Cross-system integration

## References

- Research: [knowledge/lessons/claude-skills-analysis.md](../../knowledge/lessons/claude-skills-analysis.md)
- Simon Willison: [claude-skills](https://github.com/simonw/claude-skills)
- Article: [simonwillison.net/2025/Oct/10/claude-skills/](https://simonwillison.net/2025/Oct/10/claude-skills/)
