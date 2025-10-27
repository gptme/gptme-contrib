# Creating Skills Guide

This guide explains how to create new skills for the gptme skills system.

## Overview

A skill is a complete capability package that provides:
- Comprehensive prompts explaining the domain
- Executable tools for the LLM to use
- Examples demonstrating usage
- Reference documentation

## Quick Start

### 1. Create Skill Directory

```bash
cd gptme-contrib/skills/
mkdir your-skill-name
cd your-skill-name
```

### 2. Create SKILL.md

```bash
cat > SKILL.md << 'EOF'
---
name: your-skill-name
keywords:
  - keyword1
  - keyword2
description: |
  Brief description of what this skill does
tools:
  - tool1.py
  - tool2.py
version: "1.0.0"
author: "Your Name"
status: active
---

# Your Skill Name

[Comprehensive description of the skill and its capabilities]

## Available Tools

### tool1.py
Description of what tool1 does and how to use it.

### tool2.py
Description of what tool2 does and how to use it.

## Examples

[Usage examples demonstrating the skill]

## Best Practices

[Guidelines for using this skill effectively]
EOF
```

### 3. Create Tools Directory

```bash
mkdir scripts
```

### 4. Add Tool Scripts

```python
# scripts/tool1.py
#!/usr/bin/env python3
"""Tool 1 description."""

import argparse

def main():
    parser = argparse.ArgumentParser(description="Tool 1")
    parser.add_argument("input", help="Input parameter")
    args = parser.parse_args()

    # Tool implementation
    result = process(args.input)
    print(result)

def process(input_data):
    # Your tool logic here
    return f"Processed: {input_data}"

if __name__ == "__main__":
    main()
```

Make tools executable:
```bash
chmod +x scripts/*.py
```

### 5. Add Examples

```bash
mkdir examples
cat > examples/example1.md << 'EOF'
# Example 1: Basic Usage

## Scenario
[Describe the use case]

## Steps
1. [Step 1]
2. [Step 2]
3. [Step 3]

## Expected Output
```
[Expected output]
EOF

### 6. Test Your Skill

```bash
# Validate skill structure
python3 ../discovery.py validate your-skill-name

# List skills to verify it's discoverable
python3 ../discovery.py list

# Show skill details
python3 ../discovery.py show your-skill-name
```

### 7. Test Tools Independently

```bash
# Test each tool script
python3 scripts/tool1.py test-input
python3 scripts/tool2.py test-input
```

## Skill Structure Reference
your-skill/
├── SKILL.md           # Main skill description with metadata
├── scripts/           # Executable tools
│   ├── tool1.py
│   └── tool2.py
├── examples/          # Usage examples
│   ├── example1.md
│   └── example2.md
└── reference/         # Optional: Additional docs
    └── guide.md

## Best Practices

### 1. Clear Naming
- Use kebab-case for skill names
- Choose descriptive, specific names
- Avoid generic names like "utilities"

### 2. Focused Keywords
- 3-5 keywords per skill
- Specific but not too narrow
- Think about how users will phrase requests

### 3. Comprehensive SKILL.md
- Explain what the skill does (200-500 lines)
- Describe each tool and its purpose
- Provide multiple usage examples
- Include best practices and common patterns

### 4. Reliable Tools
- Test tools independently
- Handle errors gracefully
- Provide clear error messages
- Document tool dependencies

### 5. Good Examples
- Cover common use cases
- Show realistic scenarios
- Include expected outputs
- Demonstrate best practices

## Common Pitfalls

### ❌ Avoid
- Generic skill names ("helpers", "utilities")
- Too many tools in one skill (split into multiple skills)
- Tools with undocumented dependencies
- Missing error handling in tools
- Examples without expected outputs
- Keywords too broad ("data") or too narrow ("pandas==1.5.3")

### ✅ Do
- Specific domain focus (data-analysis, web-scraping)
- 2-5 tools per skill
- Document all dependencies in metadata
- Comprehensive error handling
- Clear expected outputs in examples
- Balanced keywords (data, analysis, pandas)

## Integration with gptme

### Auto-Inclusion
Skills are automatically included when keywords match:

1. User mentions keywords in conversation
2. gptme searches skill registry
3. Matching skills loaded into context
4. Tools become available to LLM

### Manual Loading
Users can manually load skills:
/skill show your-skill-name
/skill search analysis

### Token Budget
- Skills are larger than lessons (200-500 lines vs 30-50)
- Only relevant skills loaded (keyword matching)
- Monitor context usage with multiple skills

## Testing Checklist

Before submitting a skill:

- [ ] SKILL.md has valid YAML frontmatter
- [ ] All required metadata fields present
- [ ] Keywords are specific and relevant
- [ ] Tools are executable and tested
- [ ] Examples work as documented
- [ ] Dependencies documented
- [ ] Error handling implemented
- [ ] Validation passes: `python3 ../discovery.py validate your-skill`

## Skill Templates

### Minimal Skill

A minimal skill includes:
- SKILL.md with metadata and description
- At least one tool script
- One usage example

### Complete Skill

A complete skill includes:
- SKILL.md with full metadata
- 2-5 tool scripts
- Multiple examples
- Reference documentation
- Comprehensive error handling
- Dependency documentation

## Next Steps

After creating your skill:

1. **Test locally**: Validate and test all tools
2. **Submit PR**: Create PR to gptme-contrib
3. **Documentation**: Update skills/README.md with your skill
4. **Community**: Share usage patterns and feedback

## Resources

- **Schema**: See SCHEMA.md for complete metadata reference
- **Examples**: Check existing skills (coming in Phase 4.2)
- **Discovery**: See discovery.py for implementation
- **Research**: Read knowledge/lessons/claude-skills-analysis.md

## Getting Help

- Create issue in gptme-contrib
- Check existing skills for patterns
- Review Claude Skills examples

---

**Created**: 2025-10-27
**Phase**: 4.1 Infrastructure
**Next**: Phase 4.2 will add first skill examples
