#!/usr/bin/env python3
"""Skill discovery and management for gptme skills system."""

from pathlib import Path
from typing import Dict, List, Optional
import yaml


class Skill:
    """Represents a gptme skill with metadata and content."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.metadata: Dict = {}
        self.content: str = ""
        self._load()

    def _load(self):
        """Load skill from SKILL.md file."""
        skill_file = self.path / "SKILL.md"
        if not skill_file.exists():
            raise ValueError(f"SKILL.md not found in {self.path}")

        content = skill_file.read_text()

        # Extract YAML frontmatter
        if content.startswith("---\n"):
            try:
                _, frontmatter, body = content.split("---\n", 2)
                self.metadata = yaml.safe_load(frontmatter) or {}
                self.content = body.strip()
            except ValueError:
                self.content = content
                self.metadata = {}
        else:
            self.content = content
            self.metadata = {}

    @property
    def keywords(self) -> List[str]:
        """Get skill keywords for auto-inclusion."""
        keywords = self.metadata.get("keywords", [])
        return list(keywords) if keywords else []

    @property
    def description(self) -> str:
        """Get skill description."""
        desc = self.metadata.get("description", "No description")
        return str(desc)

    @property
    def tools(self) -> List[str]:
        """Get list of tool scripts."""
        tools = self.metadata.get("tools", [])
        return list(tools) if tools else []

    @property
    def status(self) -> str:
        """Get skill status (active, experimental, deprecated)."""
        status = self.metadata.get("status", "active")
        return str(status)

    @property
    def version(self) -> Optional[str]:
        """Get skill version."""
        return self.metadata.get("version")

    def matches_keywords(self, text: str) -> bool:
        """Check if skill keywords appear in text."""
        text_lower = text.lower()
        return any(keyword.lower() in text_lower for keyword in self.keywords)

    def __repr__(self):
        return f"Skill(name={self.name}, keywords={self.keywords})"


class SkillRegistry:
    """Registry for discovering and managing skills."""

    def __init__(self, skills_dir: Optional[Path] = None):
        if skills_dir is None:
            # Default to gptme-contrib/skills/
            skills_dir = Path(__file__).parent
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, Skill] = {}
        self._discover_skills()

    def _discover_skills(self):
        """Discover all skills in the skills directory."""
        if not self.skills_dir.exists():
            return

        for item in self.skills_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                try:
                    skill = Skill(item)
                    # Only load active skills by default
                    if skill.status == "active":
                        self.skills[skill.name] = skill
                except Exception as e:
                    print(f"Warning: Failed to load skill {item.name}: {e}")

    def list_skills(self) -> List[Skill]:
        """List all available skills."""
        return list(self.skills.values())

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self.skills.get(name)

    def find_skills_by_keywords(self, text: str) -> List[Skill]:
        """Find skills matching keywords in text."""
        return [skill for skill in self.skills.values() if skill.matches_keywords(text)]

    def get_skill_content(self, name: str) -> Optional[str]:
        """Get the full content of a skill."""
        skill = self.get_skill(name)
        return skill.content if skill else None


def main():
    """CLI interface for skill management."""
    import argparse

    parser = argparse.ArgumentParser(description="Manage gptme skills")
    parser.add_argument("command", choices=["list", "show", "validate"])
    parser.add_argument("skill_name", nargs="?", help="Skill name for show/validate")

    args = parser.parse_args()

    registry = SkillRegistry()

    if args.command == "list":
        print("Available skills:")
        for skill in registry.list_skills():
            print(f"  {skill.name:20s} - {skill.description}")
            print(f"    Keywords: {', '.join(skill.keywords)}")
            print(f"    Tools: {', '.join(skill.tools)}")
            if skill.version:
                print(f"    Version: {skill.version}")
            print()

    elif args.command == "show":
        if not args.skill_name:
            print("Error: skill_name required for show command")
            return 1

        maybe_skill = registry.get_skill(args.skill_name)
        if maybe_skill is None:
            print(f"Error: Skill '{args.skill_name}' not found")
            return 1

        skill = maybe_skill
        print(f"Skill: {skill.name}")
        print(f"Description: {skill.description}")
        print(f"Keywords: {', '.join(skill.keywords)}")
        print(f"Tools: {', '.join(skill.tools)}")
        print(f"Status: {skill.status}")
        if skill.version:
            print(f"Version: {skill.version}")
        print("\nContent:")
        print(skill.content)

    elif args.command == "validate":
        if not args.skill_name:
            print("Error: skill_name required for validate command")
            return 1

        maybe_skill = registry.get_skill(args.skill_name)
        if maybe_skill is None:
            print(f"Error: Skill '{args.skill_name}' not found")
            return 1

        skill = maybe_skill
        # Validate required fields
        errors = []
        if not skill.metadata.get("name"):
            errors.append("Missing required field: name")
        if not skill.metadata.get("keywords"):
            errors.append("Missing required field: keywords")
        if not skill.metadata.get("description"):
            errors.append("Missing required field: description")
        if not skill.metadata.get("tools"):
            errors.append("Missing required field: tools")

        if errors:
            print(f"Validation failed for {skill.name}:")
            for error in errors:
                print(f"  - {error}")
            return 1
        else:
            print(f"✓ Skill {skill.name} is valid")
            return 0

    return 0


if __name__ == "__main__":
    exit(main())
