# Contributing to gptme-contrib

Thank you for considering contributing to gptme-contrib! This guide will help you get started.

## Adding New Tools

There are two ways to contribute tools:

### 1. Script Tools (Recommended)

Script tools are standalone scripts that can be run via the shell tool. This is the recommended approach as it:
- Keeps dependencies isolated
- Makes testing easier
- Allows independent use
- Simplifies maintenance

Example script tool in Python, using `uv` script dependencies:

```python
#!/usr/bin/env -S uv run
# dependencies = [
#   "requests>=2.31.0",
#   "rich>=13.7.0",
# ]

import sys
from rich import print

def main():
    print("[bold green]Hello from a script tool![/bold green]")

if __name__ == "__main__":
    main()
```

Requirements:
1. Place in the [`scripts/`](./scripts) directory
2. Use shebang: `#!/usr/bin/env -S uv run`
3. Declare dependencies in comments
4. Include basic documentation
5. Handle errors gracefully

### 2. Custom Tools

For cases where you need deeper gptme integration (e.g., for attaching files/images), you can create a custom tool in [`tools/`](./tools).

See the [custom tools documentation](https://gptme.org/docs/custom_tool.html) for details.

## Testing

Testing is encouraged but not required. If you want to add tests:

1. Create a test file in the `tests/` directory
2. Use pytest for testing Python tools

## Pull Request Process

1. Fork the repository
2. Create a feature branch
3. Add your tool and documentation
4. Submit a pull request

We aim to review PRs within a few days.

## Code Style

- Follow PEP 8
- Use type hints where helpful
- Keep it simple
- Handle errors gracefully
- Document assumptions

## Questions?

Feel free to:
- Open an issue for questions
- Join our [Discord](https://discord.gg/NMaCmmkxWv)
- Ask in the [Discussions](https://github.com/ErikBjare/gptme-contrib/discussions)
