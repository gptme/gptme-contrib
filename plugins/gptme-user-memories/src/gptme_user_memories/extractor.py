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

# Patterns that indicate an autonomous agent session (not a personal user session)
AUTONOMOUS_PATTERNS = [
    "autonomous",
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
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    content = str(msg.get("content", ""))
                    if any(
                        pat.lower() in content.lower() for pat in AUTONOMOUS_PATTERNS
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
                    if isinstance(content, list):
                        continue
                    content_str = str(content)
                    if any(
                        pat.lower() in content_str.lower()
                        for pat in AUTONOMOUS_PATTERNS
                    ):
                        return True
                    # First real user message found and not autonomous → it's personal
                    if len(content_str) > 20:
                        return False
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    config_path = Path.home() / ".config/gptme/config.toml"
    if not config_path.exists():
        return None

    try:
        config = tomllib.loads(config_path.read_text())
    except Exception:
        return None

    env_section = config.get("env", {})
    if isinstance(env_section, dict):
        value = env_section.get("ANTHROPIC_API_KEY")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def extract_facts(
    conversation_text: str, model: str = "claude-haiku-4-5-20251001"
) -> list[str]:
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

    if "NO_NEW_FACTS" in output or not output:
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
    for line in memories_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
    return facts


def save_memories(memories_file: Path, facts: list[str]) -> None:
    """Save memories to the markdown file."""
    memories_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# User Memories\n\n"
        "Facts about the user extracted from past gptme conversations.\n"
        f"Last updated: {datetime.now().strftime('%Y-%m-%d')}\n\n"
    )
    body = "\n".join(f"- {fact}" for fact in sorted(facts))
    memories_file.write_text(header + body + "\n")


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
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    """Extract facts from a single gptme conversation log directory.

    Returns list of new facts found (empty if session is autonomous or nothing found).
    """
    conv_file = logdir / "conversation.jsonl"
    if not conv_file.exists():
        return []

    sentinel = logdir / SENTINEL_FILENAME
    if not force and sentinel.exists():
        return []

    if is_autonomous_session(conv_file):
        if not force and not dry_run:
            sentinel.touch()
        return []

    text = get_user_messages(conv_file)
    if len(text) < 50:
        if not force and not dry_run:
            sentinel.touch()
        return []

    facts = extract_facts(text, model=model)

    if not dry_run:
        sentinel.touch()

    return facts


def run_batch(
    days: int = DEFAULT_DAYS,
    limit: int = 30,
    force: bool = False,
    dry_run: bool = False,
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    """Scan recent gptme and Claude Code sessions and extract user facts.

    Returns list of all new facts found.
    """
    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    all_new_facts: list[str] = []
    processed = 0

    # gptme logs
    if LOGS_DIR.exists():
        for log_dir in sorted(
            LOGS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            if not log_dir.is_dir():
                continue
            if log_dir.stat().st_mtime < cutoff_ts:
                break
            conv_file = log_dir / "conversation.jsonl"
            if not conv_file.exists():
                continue
            sentinel = log_dir / SENTINEL_FILENAME
            if not force and sentinel.exists():
                continue

            if is_autonomous_session(conv_file):
                if not force and not dry_run:
                    sentinel.touch()
                continue

            text = get_user_messages(conv_file)
            if len(text) < 50:
                if not force and not dry_run:
                    sentinel.touch()
                continue

            facts = extract_facts(text, model=model)
            all_new_facts.extend(facts)

            if not dry_run:
                sentinel.touch()

            processed += 1
            if processed >= limit:
                break

    # Claude Code logs
    if CC_LOGS_DIR.exists():
        for proj_dir in sorted(CC_LOGS_DIR.iterdir()):
            if not proj_dir.is_dir():
                continue  # skip non-directory entries (e.g. .DS_Store)
            if processed >= limit:
                break
            for jsonl_file in sorted(
                proj_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            ):
                if processed >= limit:
                    break
                if jsonl_file.stat().st_mtime < cutoff_ts:
                    break  # files sorted newest-first; remaining are all stale
                sentinel = jsonl_file.with_suffix(".memories-extracted")
                if not force and sentinel.exists():
                    continue

                if is_cc_autonomous_session(jsonl_file):
                    if not force and not dry_run:
                        sentinel.touch()
                    continue

                text = get_cc_user_messages(jsonl_file)
                if len(text) < 50:
                    if not force and not dry_run:
                        sentinel.touch()
                    continue

                facts = extract_facts(text, model=model)
                all_new_facts.extend(facts)

                if not dry_run:
                    sentinel.touch()

                processed += 1

    return all_new_facts
