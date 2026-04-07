from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TextIO

import yaml  # type: ignore[import-untyped]


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

    metadata = yaml.safe_load(raw_metadata) if raw_metadata else {}
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
