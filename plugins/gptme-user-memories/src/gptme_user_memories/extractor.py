"""User memory extraction from gptme and Claude Code conversation logs.

Scans recent conversations, uses Claude to extract key facts about the user,
and stores them in ~/.local/share/gptme/user-memories.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

LOGS_DIR = Path.home() / ".local/share/gptme/logs"
CC_LOGS_DIR = Path.home() / ".claude/projects"
USER_MEMORIES_FILE = Path.home() / ".local/share/gptme/user-memories.md"
SENTINEL_FILENAME = ".memories-extracted"
DEFAULT_DAYS = 14
MAX_CONV_CHARS = 8000
MAX_MSG_CHARS = 400
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Patterns that indicate an autonomous agent session (not a personal user session)
AUTONOMOUS_PATTERNS = [
    "You are Bob",
    "You are Agent",
    "running in autonomous mode",
    "gptme-prompt-",
    "autonomous session",
    "starting an autonomous work session",
    "project monitoring session",
]

EXTRACTION_PROMPT = """Analyze this conversation to extract key facts about the USER (not the AI assistant).

Look for facts that would help personalize future responses:
- Technical preferences (languages, frameworks, editors, workflows)
- Communication style (terse vs detailed, what they find helpful)
- Ongoing projects and their goals
- Personal facts relevant to work (timezone, company, role)
- Implicit preferences from how they give feedback or corrections
- Recurring topics or concerns

Do NOT extract:
- Things the AI said
- Common/generic preferences (e.g., "prefers code that works")
- Facts already obvious from context (e.g., "uses gptme")
- Speculative or uncertain information
- Private sensitive information

Format: Simple markdown list, one fact per line starting with "- ".
If nothing useful found, respond with exactly: NO_NEW_FACTS

Conversation (user messages only):
{conversation}"""


def is_autonomous_session(conv_file: Path) -> bool:
    """Check if a gptme session is an autonomous agent run (not an interactive user session)."""
    try:
        with open(conv_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    content = msg.get("content", "")
                    # Extract text from list-typed content (consistent with get_user_messages)
                    if isinstance(content, list):
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        if not text_parts:
                            continue  # pure tool-result, no text to check
                        content_str = " ".join(text_parts)
                    else:
                        content_str = str(content)
                    if any(
                        pat.lower() in content_str.lower()
                        for pat in AUTONOMOUS_PATTERNS
                    ):
                        return True
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass
    return False


def is_cc_autonomous_session(jsonl_file: Path) -> bool:
    """Check if a Claude Code session is an autonomous agent run."""
    try:
        with open(jsonl_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") != "user":
                        continue
                    content = msg.get("message", {}).get("content", "")
                    # Extract text from mixed-content messages (consistent with get_cc_user_messages)
                    if isinstance(content, list):
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        if not text_parts:
                            continue  # pure tool-result message, no text to check
                        content_str = " ".join(text_parts)
                    else:
                        content_str = str(content)
                    if any(
                        pat.lower() in content_str.lower()
                        for pat in AUTONOMOUS_PATTERNS
                    ):
                        return True
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass
    return False


def get_user_messages(conv_file: Path) -> str:
    """Extract user messages from a gptme conversation log."""
    messages: list[str] = []
    try:
        with open(conv_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        content = " ".join(text_parts)
                    content = str(content).strip()
                    if content and len(content) > 10:
                        messages.append(content[:MAX_MSG_CHARS])
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        return ""
    text = "\n\n".join(messages)
    return text[:MAX_CONV_CHARS]


def get_cc_user_messages(jsonl_file: Path) -> str:
    """Extract user messages from a Claude Code session JSONL file."""
    messages: list[str] = []
    try:
        with open(jsonl_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") != "user":
                        continue
                    content = msg.get("message", {}).get("content", "")
                    # Skip pure tool-result messages; extract text from mixed-content
                    if isinstance(content, list):
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        if not text_parts:
                            continue  # pure tool-result message, no user text
                        content = " ".join(text_parts)
                    content = str(content).strip()
                    if content and len(content) > 10:
                        messages.append(content[:MAX_MSG_CHARS])
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        return ""
    text = "\n\n".join(messages)
    return text[:MAX_CONV_CHARS]


def _get_anthropic_api_key() -> str | None:
    """Get Anthropic API key from env or gptme config.toml."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return api_key

    config_path = Path.home() / ".config/gptme/config.toml"
    if not config_path.exists():
        return None

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("user_memories: failed to parse %s: %s", config_path, e)
        return None

    env_section = config.get("env", {})
    if isinstance(env_section, dict):
        value = env_section.get("ANTHROPIC_API_KEY")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def extract_facts(conversation_text: str, model: str = DEFAULT_MODEL) -> list[str]:
    """Use Claude to extract user facts from conversation text."""
    if len(conversation_text.strip()) < 50:
        return []

    import anthropic

    api_key = _get_anthropic_api_key()
    if not api_key:
        logger.warning(
            "user_memories: no ANTHROPIC_API_KEY found in env or config.toml — skipping extraction"
        )
        return []

    client = anthropic.Anthropic(api_key=api_key)
    prompt = EXTRACTION_PROMPT.format(conversation=conversation_text)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        output = block.text.strip() if hasattr(block, "text") else ""
    except Exception as e:
        logger.warning("user_memories: API call failed: %s", e)
        return []

    if output.strip() == "NO_NEW_FACTS" or not output:
        return []

    facts = []
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            fact = line[2:].strip()
            if fact:
                facts.append(fact)
    return facts


def load_existing_memories(memories_file: Path) -> list[str]:
    """Load existing memories from the memories file."""
    if not memories_file.exists():
        return []
    facts = []
    for line in memories_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
    return facts


def save_memories(memories_file: Path, facts: list[str]) -> None:
    """Save memories to the markdown file using an atomic write."""
    memories_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# User Memories\n\n"
        "Facts about the user extracted from past gptme conversations.\n"
        f"Last updated: {datetime.now().strftime('%Y-%m-%d')}\n\n"
    )
    body = "\n".join(f"- {fact}" for fact in sorted(facts, key=str.casefold))
    # Write to a unique temp file in the same directory, then atomically rename to
    # avoid partial writes corrupting the accumulated memories file on interruption.
    # PID in the name prevents concurrent callers (hook + CLI) from colliding.
    tmp = memories_file.with_name(f"{memories_file.stem}.{os.getpid()}.tmp")
    replaced = False
    try:
        tmp.write_text(header + body + "\n", encoding="utf-8")
        tmp.replace(memories_file)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


def merge_facts(existing: list[str], new_facts: list[str]) -> list[str]:
    """Merge new facts with existing ones, deduplicating by normalized form."""
    normalized_existing = {f.lower().strip() for f in existing}
    merged = list(existing)
    for fact in new_facts:
        if fact.lower().strip() not in normalized_existing:
            merged.append(fact)
            normalized_existing.add(fact.lower().strip())
    return merged


def process_logdir(
    logdir: Path,
    force: bool = False,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
) -> list[str] | None:
    """Extract facts from a single gptme conversation log directory.

    Returns:
        list[str]: facts found (may be empty if API returned nothing useful).
            Indicates the API was called — counts toward the session limit.
        None: session was filtered without an API call (autonomous, too short,
            or already processed). Does NOT count toward the session limit.
    """
    conv_file = logdir / "conversation.jsonl"
    if not conv_file.exists():
        return None

    sentinel = logdir / SENTINEL_FILENAME
    if not force and sentinel.exists():
        return None

    if is_autonomous_session(conv_file):
        if not force and not dry_run:
            sentinel.touch()
        return None

    text = get_user_messages(conv_file)
    if len(text) < 50:
        if not force and not dry_run:
            sentinel.touch()
        return None

    facts = extract_facts(text, model=model)

    if not dry_run:
        sentinel.touch()

    return facts


def process_cc_logfile(
    jsonl_file: Path,
    force: bool = False,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
) -> list[str] | None:
    """Extract facts from a single Claude Code session JSONL file.

    Returns:
        list[str]: facts found (may be empty if API returned nothing useful).
            Indicates the API was called — counts toward the session limit.
        None: session was filtered without an API call (autonomous, too short,
            or already processed). Does NOT count toward the session limit.
    """
    sentinel = jsonl_file.with_suffix(".memories-extracted")
    if not force and sentinel.exists():
        return None

    if is_cc_autonomous_session(jsonl_file):
        if not force and not dry_run:
            sentinel.touch()
        return None

    text = get_cc_user_messages(jsonl_file)
    if len(text) < 50:
        if not force and not dry_run:
            sentinel.touch()
        return None

    facts = extract_facts(text, model=model)

    if not dry_run:
        sentinel.touch()

    return facts


def run_batch(
    days: int = DEFAULT_DAYS,
    limit: int = 30,
    force: bool = False,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
) -> list[str]:
    """Scan recent gptme and Claude Code sessions and extract user facts.

    Returns list of all new facts found. Call save_memories() to persist them.
    """
    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    all_new_facts: list[str] = []
    # Use per-source limits so neither source starves the other
    per_source_limit = max(1, limit // 2)
    gptme_processed = 0
    cc_processed = 0

    def _safe_mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # gptme logs
    if LOGS_DIR.exists():
        for log_dir in sorted(LOGS_DIR.iterdir(), key=_safe_mtime, reverse=True):
            if not log_dir.is_dir():
                continue
            if _safe_mtime(log_dir) < cutoff_ts:
                break
            if not (log_dir / "conversation.jsonl").exists():
                continue
            # Pre-check sentinel so sentinel-skipped sessions don't count toward limit
            if not force and (log_dir / SENTINEL_FILENAME).exists():
                continue

            facts = process_logdir(log_dir, force=force, dry_run=dry_run, model=model)
            if facts is not None:
                all_new_facts.extend(facts)
                gptme_processed += 1
            if gptme_processed >= per_source_limit:
                break

    # Claude Code logs
    if CC_LOGS_DIR.exists():
        for proj_dir in sorted(CC_LOGS_DIR.iterdir(), key=_safe_mtime, reverse=True):
            if not proj_dir.is_dir():
                continue  # skip non-directory entries (e.g. .DS_Store)
            if _safe_mtime(proj_dir) < cutoff_ts:
                break  # dirs sorted newest-first; remaining are all stale
            if cc_processed >= per_source_limit:
                break
            for jsonl_file in sorted(
                proj_dir.glob("*.jsonl"), key=_safe_mtime, reverse=True
            ):
                if cc_processed >= per_source_limit:
                    break
                if _safe_mtime(jsonl_file) < cutoff_ts:
                    break  # files sorted newest-first; remaining are all stale
                # Pre-check sentinel so sentinel-skipped sessions don't count toward limit
                if not force and jsonl_file.with_suffix(".memories-extracted").exists():
                    continue

                facts = process_cc_logfile(
                    jsonl_file, force=force, dry_run=dry_run, model=model
                )
                if facts is not None:
                    all_new_facts.extend(facts)
                    cc_processed += 1

    return all_new_facts


def main() -> None:
    """CLI entry point for backfilling user memories from historical sessions."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract user facts from past gptme/Claude Code sessions and store them in user-memories.md.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="How many days back to scan (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max sessions to process per run (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and print facts but don't save or touch sentinels",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process sessions that already have a sentinel file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=USER_MEMORIES_FILE,
        help="Output memories file (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Anthropic model to use for fact extraction (default: %(default)s)",
    )
    args = parser.parse_args()

    new_facts = run_batch(
        days=args.days,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        model=args.model,
    )

    if not new_facts:
        print("No new facts found.")
        return

    if args.dry_run:
        print(f"Dry run — would add up to {len(new_facts)} facts (before dedup):")
        for fact in new_facts:
            print(f"  - {fact}")
        return

    existing = load_existing_memories(args.output)
    merged = merge_facts(existing, new_facts)
    new_count = len(merged) - len(existing)
    save_memories(args.output, merged)
    print(f"Added {new_count} new facts (total: {len(merged)}) → {args.output}")
