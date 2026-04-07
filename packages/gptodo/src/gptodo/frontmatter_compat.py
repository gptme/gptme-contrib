from __future__ import annotations

import importlib.util
import sys
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


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(f"{_DELIM}\n") and text != _DELIM:
        return {}, text

    parts = text.split(_DELIM, 2)
    if len(parts) < 3:
        return {}, text

    raw_metadata = parts[1].strip()
    body = parts[2]
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
    repo_root = Path(__file__).resolve().parents[5]
    candidate = (
        repo_root
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "frontmatter"
        / "__init__.py"
    )
    if not candidate.exists():
        return None
    spec = importlib.util.spec_from_file_location("python_frontmatter_real", candidate)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "load") and hasattr(module, "dumps") and hasattr(module, "Post"):
        return module
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
