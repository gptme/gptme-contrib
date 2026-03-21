"""User memory extraction from gptme and Claude Code conversation logs.

Scans recent conversations, uses Claude to extract key facts about the user,
and stores them in ~/.config/gptme/user-memories/facts.md — a shared location
readable by all gptme agents on the same machine.
"""

from __future__ import annotations

import json
import logging
import math
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
CC_SENTINEL_DIR = Path.home() / ".local/share/gptme/cc-sentinels"
# Shared location readable by all agents on the same machine.
# Previously: ~/.local/share/gptme/user-memories.md
USER_MEMORIES_DIR = Path.home() / ".config/gptme/user-memories"
USER_MEMORIES_FILE = USER_MEMORIES_DIR / "facts.md"
SENTINEL_FILENAME = ".memories-extracted"
DEFAULT_DAYS = 14
MAX_CONV_CHARS = 8000
MAX_MSG_CHARS = 400
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Patterns that indicate an autonomous agent session (not a personal user session)
AUTONOMOUS_PATTERNS = [
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

CATEGORIZED_EXTRACTION_PROMPT = """Analyze this conversation to extract key facts about the USER (not the AI assistant).

Organize facts into these categories:

## preferences
Technical preferences (languages, frameworks, editors, workflows, communication style).

## projects
Ongoing projects, their goals and current status.

## personal
Personal facts relevant to work (timezone, company, role, location).

Do NOT extract:
- Things the AI said
- Common/generic preferences (e.g., "prefers code that works")
- Facts already obvious from context (e.g., "uses gptme")
- Speculative or uncertain information
- Private sensitive information

Format: Use the section headers above, with a markdown list under each.
Omit sections that have no facts. If nothing useful found at all, respond with: NO_NEW_FACTS

Conversation (user messages only):
{conversation}"""

# Category file names and display titles
MEMORY_CATEGORIES: dict[str, str] = {
    "preferences": "User Preferences",
    "projects": "User Projects",
    "personal": "Personal Context",
}


def is_autonomous_session(conv_file: Path) -> bool:
    """Check if a gptme session is an autonomous agent run (not an interactive user session)."""
    try:
        with open(conv_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    # Only check system/user messages — assistant messages may
                    # *explain* autonomous mode, causing false-positive matches.
                    if msg.get("role") not in ("system", "user"):
                        continue
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


def extract_facts(
    conversation_text: str, model: str = DEFAULT_MODEL
) -> list[str] | None:
    """Use Claude to extract user facts from conversation text.

    Returns:
        list[str]: facts extracted (may be empty if no new facts found).
        None: API call failed (transient error) — caller should NOT touch sentinel.
    """
    if len(conversation_text.strip()) < 50:
        return []

    import anthropic  # type: ignore[import-not-found]

    api_key = _get_anthropic_api_key()
    if not api_key:
        logger.warning(
            "user_memories: no ANTHROPIC_API_KEY found in env or config.toml — skipping extraction"
        )
        return None

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
        logger.warning(
            "user_memories: API call failed: %s — session will be retried", e
        )
        return None

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


def parse_categorized_output(output: str) -> dict[str, list[str]]:
    """Parse categorized LLM output into a dict of {category: [facts]}.

    Recognizes section headers like ``## preferences`` and collects
    bullet-point facts under each. Unknown categories are silently ignored
    (only ``preferences``, ``projects``, ``personal`` are kept).
    """
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            category = stripped[3:].strip().lower()
            if category in MEMORY_CATEGORIES:
                current = category
                result.setdefault(current, [])
            else:
                current = None  # skip unknown categories
        elif stripped.startswith("- ") and current is not None:
            fact = stripped[2:].strip()
            if fact:
                result[current].append(fact)
    return result


def extract_categorized_facts(
    conversation_text: str, model: str = DEFAULT_MODEL
) -> dict[str, list[str]] | None:
    """Use Claude to extract user facts organized by category.

    Returns:
        dict[str, list[str]]: facts per category (may be empty dicts).
        None: API call failed — caller should NOT touch sentinel.
    """
    if len(conversation_text.strip()) < 50:
        return {}

    import anthropic  # type: ignore[import-not-found]

    api_key = _get_anthropic_api_key()
    if not api_key:
        logger.warning(
            "user_memories: no ANTHROPIC_API_KEY found in env or config.toml — skipping extraction"
        )
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = CATEGORIZED_EXTRACTION_PROMPT.format(conversation=conversation_text)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,  # higher than flat extraction: multi-section format needs more tokens
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        output = block.text.strip() if hasattr(block, "text") else ""
    except Exception as e:
        logger.warning(
            "user_memories: API call failed: %s — session will be retried", e
        )
        return None

    if output.strip() == "NO_NEW_FACTS" or not output:
        return {}

    return parse_categorized_output(output)


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
    """Merge new facts with existing ones, deduplicating by normalized form.

    Also deduplicates within ``existing`` itself, so duplicates introduced via
    manual editing of the memories file are cleaned up on the next write.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for fact in existing + new_facts:
        normalized = fact.lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            merged.append(fact)
    return merged


def save_categorized_memories(
    memories_dir: Path, categorized: dict[str, list[str]]
) -> dict[str, int]:
    """Save categorized facts to per-category files in memories_dir.

    Each category gets its own markdown file (e.g. preferences.md, projects.md,
    personal.md). Facts are merged with any existing content and deduplicated.
    Categories with no new facts are left unchanged.

    Returns:
        dict[str, int]: net-new fact count per category (len(merged) - len(existing)).
    """
    new_counts: dict[str, int] = {}
    for category, new_facts in categorized.items():
        if not new_facts:
            continue
        title = MEMORY_CATEGORIES.get(category, category.capitalize())
        cat_file = memories_dir / f"{category}.md"
        existing = load_existing_memories(cat_file)
        merged = merge_facts(existing, new_facts)
        if merged == existing:
            continue  # all facts already present — skip write
        # Reuse save_memories but target the category file, overriding title
        # by temporarily building the content directly.
        memories_dir.mkdir(parents=True, exist_ok=True)
        header = (
            f"# {title}\n\n"
            f"Facts about the user extracted from past gptme conversations.\n"
            f"Last updated: {datetime.now().strftime('%Y-%m-%d')}\n\n"
        )
        body = "\n".join(f"- {fact}" for fact in sorted(merged, key=str.casefold))
        tmp = cat_file.with_name(f"{cat_file.stem}.{os.getpid()}.tmp")
        replaced = False
        try:
            tmp.write_text(header + body + "\n", encoding="utf-8")
            tmp.replace(cat_file)
            replaced = True
        finally:
            if not replaced:
                tmp.unlink(missing_ok=True)
        new_counts[category] = len(merged) - len(existing)
        logger.info(
            "user_memories: saved %d %s facts to %s", len(merged), category, cat_file
        )
    return new_counts


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
        None: session was not processed — either filtered (autonomous, too short,
            already processed) or API call failed. Does NOT count toward the session
            limit. Sentinel is NOT touched on API failure so the session is retried.
    """
    conv_file = logdir / "conversation.jsonl"
    if not conv_file.exists():
        return None

    sentinel = logdir / SENTINEL_FILENAME
    if not force and sentinel.exists():
        return None

    if is_autonomous_session(conv_file):
        if not dry_run:
            sentinel.touch()
        return None

    text = get_user_messages(conv_file)
    if len(text) < 50:
        if not dry_run:
            sentinel.touch()
        return None

    facts = extract_facts(text, model=model)

    if facts is None:
        # API failure — do NOT touch sentinel so the session is retried next time
        return None

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
        None: session was not processed — either filtered (autonomous, too short,
            already processed) or API call failed. Does NOT count toward the session
            limit. Sentinel is NOT touched on API failure so the session is retried.
    """
    # Store sentinel under gptme's data dir, not inside CC's own directories
    try:
        sentinel = CC_SENTINEL_DIR / jsonl_file.relative_to(CC_LOGS_DIR).with_suffix(
            ".memories-extracted"
        )
    except ValueError:
        logger.warning(
            "user_memories: %s is not under CC_LOGS_DIR %s — cannot compute sentinel path, skipping",
            jsonl_file,
            CC_LOGS_DIR,
        )
        return None
    if not force and sentinel.exists():
        return None

    def _touch_sentinel() -> None:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()

    if is_cc_autonomous_session(jsonl_file):
        if not dry_run:
            _touch_sentinel()
        return None

    text = get_cc_user_messages(jsonl_file)
    if len(text) < 50:
        if not dry_run:
            _touch_sentinel()
        return None

    facts = extract_facts(text, model=model)

    if facts is None:
        # API failure — do NOT touch sentinel so the session is retried next time
        return None

    if not dry_run:
        _touch_sentinel()

    return facts


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def run_batch_categorized(
    days: int = DEFAULT_DAYS,
    limit: int = 30,
    force: bool = False,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
) -> dict[str, list[str]]:
    """Scan recent sessions and extract user facts organized by category.

    Returns a dict of {category: [facts]}. Call save_categorized_memories() to persist.
    """
    if not _get_anthropic_api_key():
        logger.warning(
            "user_memories: ANTHROPIC_API_KEY not configured — skipping batch extraction"
        )
        return {}

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    all_categorized: dict[str, list[str]] = {}
    per_source_limit = max(1, math.ceil(limit / 2))
    gptme_processed = 0
    total_processed = 0

    def _merge_categorized(
        target: dict[str, list[str]], source: dict[str, list[str]]
    ) -> None:
        for cat, facts in source.items():
            target.setdefault(cat, []).extend(facts)

    # gptme logs
    if LOGS_DIR.exists():
        gptme_entries = sorted(
            ((mtime, p) for p in LOGS_DIR.iterdir() if (mtime := _safe_mtime(p))),
            key=lambda t: t[0],
            reverse=True,
        )
        for mtime, log_dir in gptme_entries:
            if not log_dir.is_dir():
                continue
            if mtime < cutoff_ts:
                break
            if not (log_dir / "conversation.jsonl").exists():
                continue
            if not force and (log_dir / SENTINEL_FILENAME).exists():
                continue

            conv_file = log_dir / "conversation.jsonl"
            sentinel = log_dir / SENTINEL_FILENAME
            if is_autonomous_session(conv_file):
                if not dry_run:
                    sentinel.touch()
                continue
            text = get_user_messages(conv_file)
            if len(text) < 50:
                if not dry_run:
                    sentinel.touch()
                continue
            result = extract_categorized_facts(text, model=model)
            if result is None:
                continue  # API failure — skip, don't touch sentinel
            if not dry_run:
                sentinel.touch()
            _merge_categorized(all_categorized, result)
            gptme_processed += 1
            total_processed += 1
            if gptme_processed >= per_source_limit or total_processed >= limit:
                break

    # Claude Code logs — no per-source cap here; total_processed enforces the ceiling.
    # This allows CC to absorb unused quota when gptme has fewer than per_source_limit sessions.
    if CC_LOGS_DIR.exists():
        cc_proj_entries = sorted(
            ((mtime, p) for p in CC_LOGS_DIR.iterdir() if (mtime := _safe_mtime(p))),
            key=lambda t: t[0],
            reverse=True,
        )
        for proj_mtime, proj_dir in cc_proj_entries:
            if not proj_dir.is_dir():
                continue
            if proj_mtime < cutoff_ts:
                break
            if total_processed >= limit:
                break
            jsonl_entries = sorted(
                (
                    (mtime, f)
                    for f in proj_dir.glob("*.jsonl")
                    if (mtime := _safe_mtime(f))
                ),
                key=lambda t: t[0],
                reverse=True,
            )
            for file_mtime, jsonl_file in jsonl_entries:
                if total_processed >= limit:
                    break
                if file_mtime < cutoff_ts:
                    break
                cc_sentinel = CC_SENTINEL_DIR / jsonl_file.relative_to(
                    CC_LOGS_DIR
                ).with_suffix(".memories-extracted")
                if not force and cc_sentinel.exists():
                    continue

                if is_cc_autonomous_session(jsonl_file):
                    if not dry_run:
                        cc_sentinel.parent.mkdir(parents=True, exist_ok=True)
                        cc_sentinel.touch()
                    continue
                text = get_cc_user_messages(jsonl_file)
                if len(text) < 50:
                    if not dry_run:
                        cc_sentinel.parent.mkdir(parents=True, exist_ok=True)
                        cc_sentinel.touch()
                    continue
                result = extract_categorized_facts(text, model=model)
                if result is None:
                    continue
                if not dry_run:
                    cc_sentinel.parent.mkdir(parents=True, exist_ok=True)
                    cc_sentinel.touch()
                _merge_categorized(all_categorized, result)
                total_processed += 1

    return all_categorized


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
    if not _get_anthropic_api_key():
        logger.warning(
            "user_memories: ANTHROPIC_API_KEY not configured — skipping batch extraction (set env var or add to config.toml)"
        )
        return []

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    all_new_facts: list[str] = []
    # Cap gptme at half the total limit so CC sessions aren't starved.
    # CC has no separate cap — it can absorb any unused gptme quota up to `limit`.
    # total_processed enforces the hard ceiling across both sources.
    # Use ceil so odd limits (3, 5, 7…) still give gptme a fair share.
    per_source_limit = max(1, math.ceil(limit / 2))
    gptme_processed = 0
    total_processed = 0

    # gptme logs
    if LOGS_DIR.exists():
        # Collect (mtime, path) once so sort key and cutoff check share the same stat() call.
        gptme_entries = sorted(
            ((mtime, p) for p in LOGS_DIR.iterdir() if (mtime := _safe_mtime(p))),
            key=lambda t: t[0],
            reverse=True,
        )
        for mtime, log_dir in gptme_entries:
            if not log_dir.is_dir():
                continue
            if mtime < cutoff_ts:
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
                total_processed += 1
            if gptme_processed >= per_source_limit or total_processed >= limit:
                break

    # Claude Code logs — no per-source cap here; total_processed enforces the ceiling.
    # This allows CC to absorb unused quota when gptme has fewer than per_source_limit sessions.
    if CC_LOGS_DIR.exists():
        cc_proj_entries = sorted(
            ((mtime, p) for p in CC_LOGS_DIR.iterdir() if (mtime := _safe_mtime(p))),
            key=lambda t: t[0],
            reverse=True,
        )
        for proj_mtime, proj_dir in cc_proj_entries:
            if not proj_dir.is_dir():
                continue  # skip non-directory entries (e.g. .DS_Store)
            if proj_mtime < cutoff_ts:
                break  # dirs sorted newest-first; remaining are all stale
            if total_processed >= limit:
                break
            jsonl_entries = sorted(
                (
                    (mtime, f)
                    for f in proj_dir.glob("*.jsonl")
                    if (mtime := _safe_mtime(f))
                ),
                key=lambda t: t[0],
                reverse=True,
            )
            for file_mtime, jsonl_file in jsonl_entries:
                if total_processed >= limit:
                    break
                if file_mtime < cutoff_ts:
                    break  # files sorted newest-first; remaining are all stale
                # Pre-check sentinel so sentinel-skipped sessions don't count toward limit
                cc_sentinel = CC_SENTINEL_DIR / jsonl_file.relative_to(
                    CC_LOGS_DIR
                ).with_suffix(".memories-extracted")
                if not force and cc_sentinel.exists():
                    continue

                facts = process_cc_logfile(
                    jsonl_file, force=force, dry_run=dry_run, model=model
                )
                if facts is not None:
                    all_new_facts.extend(facts)
                    total_processed += 1

    return all_new_facts


def main() -> None:
    """CLI entry point for backfilling user memories from historical sessions."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract user facts from past gptme/Claude Code sessions and store them in ~/.config/gptme/user-memories/facts.md.",
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
    parser.add_argument(
        "--categorize",
        action="store_true",
        help=(
            "Organize extracted facts into category files "
            "(preferences.md, projects.md, personal.md) "
            "instead of a single flat facts.md. "
            "Note: shares session sentinels with the default mode — use --force to "
            "(re-)process already-sentineled sessions."
        ),
    )
    args = parser.parse_args()

    if args.categorize:
        if args.output != USER_MEMORIES_FILE:
            logger.warning(
                "--output is ignored when --categorize is used; writing to %s",
                USER_MEMORIES_DIR,
            )
        categorized = run_batch_categorized(
            days=args.days,
            limit=args.limit,
            force=args.force,
            dry_run=args.dry_run,
            model=args.model,
        )
        if not categorized:
            print("No new facts found.")
            return
        if args.dry_run:
            total = sum(len(f) for f in categorized.values())
            print(f"Dry run — would add up to {total} facts (before dedup):")
            for cat, facts in categorized.items():
                print(f"  [{cat}]")
                for fact in facts:
                    print(f"    - {fact}")
            return
        new_counts = save_categorized_memories(USER_MEMORIES_DIR, categorized)
        if not new_counts:
            print("No new facts found (all already known).")
            return
        for cat, count in new_counts.items():
            print(f"  {cat}: {count} new facts → {USER_MEMORIES_DIR / cat}.md")
        return

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
