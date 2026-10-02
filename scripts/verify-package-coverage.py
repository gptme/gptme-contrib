#!/usr/bin/env python3
"""Verify that all packages in packages/ are documented in README files."""

import re
from pathlib import Path


def get_actual_packages():
    """Get list of actual package directories."""
    packages_dir = Path(__file__).parent.parent / "packages"
    packages = sorted(
        [
            d.name
            for d in packages_dir.iterdir()
            if d.is_dir() and d.name not in ["__pycache__", "__init__"]
        ]
    )
    return packages


def get_documented_packages(readme_path):
    """Extract package names from README markdown table."""
    content = readme_path.read_text()

    packages = set()

    # Match markdown table links like [package-name](./packages/package-name/)
    pattern1 = r"\[([^\]]+)\]\(\.?/?packages/([^/]+)/?\)"
    matches1 = re.findall(pattern1, content)
    packages.update(m[1] for m in matches1 if m[1] not in ["README"])

    # Match bold markdown like **package-name** (in table cells)
    # Look for bold package names followed by pipe or newline
    pattern2 = r"\|\s*\*\*([a-z0-9_-]+)\*\*"
    matches2 = re.findall(pattern2, content)
    packages.update(matches2)

    return sorted(packages)


def main():
    repo_root = Path(__file__).parent.parent
    root_readme = repo_root / "README.md"
    packages_readme = repo_root / "packages" / "README.md"

    actual = set(get_actual_packages())
    root_documented = set(get_documented_packages(root_readme))
    packages_documented = set(get_documented_packages(packages_readme))

    print(f"Found {len(actual)} actual packages in packages/")
    print(f"Found {len(root_documented)} packages documented in README.md")
    print(f"Found {len(packages_documented)} packages documented in packages/README.md")

    # Check root README coverage
    missing_from_root = actual - root_documented
    if missing_from_root:
        print(f"\n❌ Missing from root README.md: {sorted(missing_from_root)}")
    else:
        print("\n✅ All packages documented in root README.md")

    # Check packages/README.md coverage
    missing_from_packages = actual - packages_documented
    if missing_from_packages:
        print(f"❌ Missing from packages/README.md: {sorted(missing_from_packages)}")
    else:
        print("✅ All packages documented in packages/README.md")

    # Check for undocumented packages
    extra_in_root = root_documented - actual
    if extra_in_root:
        print(
            f"⚠️  Documented but not in packages/ (root README.md): {sorted(extra_in_root)}"
        )

    extra_in_packages = packages_documented - actual
    if extra_in_packages:
        print(
            f"⚠️  Documented but not in packages/ (packages/README.md): {sorted(extra_in_packages)}"
        )

    # Exit with error if any packages are missing
    if missing_from_root or missing_from_packages:
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
