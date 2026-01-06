"""Shared utilities for template validation."""
from pathlib import Path
from typing import List


def find_git_root(start_path: Path) -> Path:
    """Find the git repository root starting from given path."""
    current = start_path.resolve()
    
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
        
    raise ValueError(f"Not in a git repository: {start_path}")


def read_gitignore(root: Path) -> List[str]:
    """Read .gitignore patterns from repository."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
        
    patterns = []
    with open(gitignore) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
                
    return patterns
