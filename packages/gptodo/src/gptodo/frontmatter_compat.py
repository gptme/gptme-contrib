from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

import yaml  # type: ignore[import-untyped]


def _datetime_representer(dumper: yaml.SafeDumper, data: datetime) -> yaml.ScalarNode:
    """Serialize datetime with ISO 8601 'T' separator instead of PyYAML's default space.

    PyYAML's default timestamp serialization uses a space between the date and time,
    producing values like '2026-05-01 09:31:00+00:00' that the workspace task validator
    rejects. This representer preserves the strict ISO 8601 form.
    """
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


def _date_representer(dumper: yaml.SafeDumper, data: date) -> yaml.ScalarNode:
    """Serialize date as plain ISO 8601 (YYYY-MM-DD)."""
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


# Register on every SafeDumper variant we might encounter:
#   - yaml.SafeDumper (pure-Python, used by the compat shim)
#   - yaml.CSafeDumper (C-accelerated, used by python-frontmatter when libyaml
#     is available — different class, separate representer table)
_DUMPERS: list[Any] = [yaml.SafeDumper]
_csafe = getattr(yaml, "CSafeDumper", None)
if _csafe is not None:
    _DUMPERS.append(_csafe)
for _dumper in _DUMPERS:
    _dumper.add_representer(datetime, _datetime_representer)
    _dumper.add_representer(date, _date_representer)


class Post(dict[str, Any]):
    """Minimal python-frontmatter-compatible Post object."""

    def __init__(self, content: str = "", **metadata: Any) -> None:
        super().__init__(metadata)
        self.content = content

    @property
    def metadata(self) -> dict[str, Any]:
        return self


_DELIM = "---"
_DELIM_RE = re.compile(r"^---\s*$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(f"{_DELIM}\n") and text != _DELIM:
        return {}, text

    matches = list(_DELIM_RE.finditer(text))
    if len(matches) < 2:
        return {}, text

    raw_metadata = text[matches[0].end() : matches[1].start()].strip()
    body = text[matches[1].end() :]
    if body.startswith("\n"):
        body = body[1:]

    safe_loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    metadata = yaml.load(raw_metadata, Loader=safe_loader) if raw_metadata else {}
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise TypeError("Frontmatter must deserialize to a mapping")
    return metadata, body


def loads(text: str) -> Post:
    metadata, content = _split_frontmatter(text)
    return Post(content=content, **metadata)


def load(path_or_file: str | Path | TextIO) -> Post:
    if hasattr(path_or_file, "read"):
        return loads(path_or_file.read())
    path = Path(path_or_file)
    return loads(path.read_text(encoding="utf-8"))


def dumps(post: Post) -> str:
    metadata = dict(post.metadata)
    body = post.content
    if metadata:
        rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
        if body:
            return f"{_DELIM}\n{rendered}\n{_DELIM}\n{body}"
        return f"{_DELIM}\n{rendered}\n{_DELIM}\n"
    return body


def dump(post: Post, fd: TextIO) -> None:
    fd.write(dumps(post))


_TOP_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:(?:[ \t]|$)")


def _dump_metadata(metadata: dict[str, Any]) -> str:
    return yaml.dump(
        metadata,
        Dumper=yaml.SafeDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _key_blocks(raw_metadata: str) -> tuple[dict[str, tuple[str, str]], str] | None:
    """Split raw frontmatter into per-key ``(leading trivia, lines)`` and tail trivia.

    Blank lines and column-0 comments before a key belong to that key; indented
    lines, sequence items and trivia followed by them continue the current key.
    Returns None when a top-level line is not a plain key this parser understands.
    """
    blocks: dict[str, tuple[str, str]] = {}
    pending: list[str] = []
    key: str | None = None
    leading = ""
    body: list[str] = []
    for line in raw_metadata.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or line.startswith("#"):
            pending.append(line)
            continue
        if line[0] in " \t-":
            if key is None:
                return None
            body.extend(pending)
            pending = []
            body.append(line)
            continue
        match = _TOP_KEY_RE.match(line)
        if match is None or match.group(1) in blocks or match.group(1) == key:
            return None
        if key is not None:
            blocks[key] = (leading, "".join(body))
        key, leading, body = match.group(1), "".join(pending), [line]
        pending = []
    if key is not None:
        blocks[key] = (leading, "".join(body))
    return blocks, "".join(pending)


def _same(a: Any, b: Any) -> bool:
    # repr keeps True vs 1 and datetime vs str apart, unlike ==.
    return type(a) is type(b) and repr(a) == repr(b)


def render_update(raw_text: str, old: Any, new: Any) -> str:
    """Render ``new`` as a task file, preserving ``raw_text`` wherever unchanged.

    Unchanged keys keep their original lines (comments and quoting included),
    keys keep their order, and an unchanged body is reproduced byte-for-byte,
    trailing whitespace and final newline included. A changed key is dumped
    fresh. If the result does not load back to exactly ``new``'s metadata,
    fall back to a plain dump that still keeps key order.
    """
    old_meta, new_meta = dict(old.metadata), dict(new.metadata)
    matches = list(_DELIM_RE.finditer(raw_text)) if raw_text.startswith(f"{_DELIM}\n") else []
    # Slice by delimiter start: _DELIM_RE's trailing \s* can swallow newlines.
    closing = matches[1].start() if len(matches) >= 2 else None
    if old.content == new.content and closing is not None:
        body = raw_text[closing + len(_DELIM) :]
    elif not new.content:
        body = "\n"
    else:
        content = new.content if new.content.startswith("\n") else "\n" + new.content
        body = "\n" + content if content.endswith("\n") else "\n" + content + "\n"

    rendered: str | None = None
    parsed = None
    if closing is not None:
        parsed = _key_blocks(raw_text[len(_DELIM) + 1 : closing])
    if parsed is not None:
        blocks, tail = parsed
        parts = []
        for key, value in new_meta.items():
            leading, lines = blocks.get(key, ("", ""))
            if not (key in old_meta and _same(old_meta[key], value) and lines):
                lines = _dump_metadata({key: value})
            parts.append(leading + lines)
        rendered = "".join(parts) + tail
        safe_loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        try:
            reloaded = yaml.load(rendered, Loader=safe_loader) or {}
        except yaml.YAMLError:
            reloaded = None
        if not isinstance(reloaded, dict) or not _same(reloaded, new_meta):
            rendered = None
    if rendered is None:
        rendered = _dump_metadata(new_meta) if new_meta else ""
    if not rendered.endswith("\n"):
        rendered += "\n"
    return f"{_DELIM}\n{rendered}{_DELIM}{body}"


def _load_real_frontmatter() -> Any:
    """Try to load the real python-frontmatter package for full compatibility."""
    try:
        import frontmatter as _real_fm

        if hasattr(_real_fm, "load") and hasattr(_real_fm, "dumps") and hasattr(_real_fm, "Post"):
            return _real_fm
        return None
    except ImportError:
        return None


frontmatter = _load_real_frontmatter()
if frontmatter is None:

    class _CompatModule:
        Post = Post
        load = staticmethod(load)
        loads = staticmethod(loads)
        dump = staticmethod(dump)
        dumps = staticmethod(dumps)

    frontmatter = _CompatModule()
