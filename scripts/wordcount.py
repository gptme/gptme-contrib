#!/usr/bin/env -S uv run
# dependencies = [
#   "rich>=13.7.0",
# ]
"""
Word Counter Tool for gptme

Counts words, lines, and characters in text from files or stdin.

Usage:
    ./wordcount.py [file...]           # Count words in files
    echo "text" | ./wordcount.py       # Count words from stdin
    ./wordcount.py < file.txt          # Count words from redirected input

Example in gptme:
    Assistant: Let me count the words in README.md
    ```shell
    ./scripts/wordcount.py README.md
    ```
"""

import fileinput
import sys
from collections import Counter
from typing import Iterator

from rich import print
from rich.console import Console
from rich.table import Table

console = Console()


def count_text(text: str) -> tuple[int, int, int, Counter[str]]:
    """Count words, lines, chars, and word frequencies in text."""
    lines = text.splitlines()
    # Split on whitespace and filter out empty strings
    words = [w for w in text.split() if w]
    chars = len(text)
    word_freq = Counter(words)

    # Handle empty input
    if not text.strip():
        return 0, 0, 0, Counter()

    return len(lines) or 1, len(words), chars, word_freq


def process_input(files: list[str]) -> Iterator[tuple[str, str]]:
    """Process input from files or stdin, yield (source, content) pairs."""
    try:
        with fileinput.input(files=files if files else ("-",)) as f:
            current_file = None
            current_content: list[str] = []

            for line in f:
                if current_file != f.filename():
                    if current_file:
                        yield current_file, "".join(current_content)
                    current_file = f.filename()
                    current_content = []
                current_content.append(line)

            if current_file:
                yield current_file, "".join(current_content)
    except FileNotFoundError as e:
        print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def main() -> None:
    # Process all input
    total_lines = total_words = total_chars = 0
    results = []

    for source, content in process_input(sys.argv[1:]):
        lines, words, chars, freq = count_text(content)
        total_lines += lines
        total_words += words
        total_chars += chars

        # Get top 3 words with counts
        top_words = [f"{w}({c})" for w, c in freq.most_common(3)]

        results.append(
            {
                "source": source if source != "-" else "stdin",
                "lines": lines,
                "words": words,
                "chars": chars,
                "top_words": ", ".join(top_words) if top_words else "",
            }
        )

    # Create and print results table
    table = Table(title="Word Count Results")
    table.add_column("Source", style="cyan")
    table.add_column("Lines", justify="right", style="green")
    table.add_column("Words", justify="right", style="green")
    table.add_column("Chars", justify="right", style="green")
    table.add_column("Most Common Words", style="yellow")

    # Add rows
    for result in results:
        table.add_row(
            str(result["source"]),
            str(result["lines"]),
            str(result["words"]),
            str(result["chars"]),
            str(result["top_words"]),
        )

    # Add total row for multiple files
    if len(results) > 1:
        table.add_row(
            "Total",
            str(total_lines),
            str(total_words),
            str(total_chars),
            "",
            style="bold",
        )

    # Print the table
    console.print(table)


if __name__ == "__main__":
    main()
