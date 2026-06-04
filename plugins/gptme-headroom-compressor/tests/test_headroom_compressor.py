"""Tests for the gptme headroom compressor hook."""

from __future__ import annotations

import pytest

# ── helpers ────────────────────────────────────────────────────────


def _tool_message(content: str):
    """Build a Message-like object for testing."""
    from dataclasses import dataclass

    @dataclass
    class FakeMessage:
        role: str = "system"
        content: str = ""
        pinned: bool = False

        def replace(self, **kwargs):
            return FakeMessage(
                **{  # type: ignore
                    **{
                        "role": self.role,
                        "content": self.content,
                        "pinned": self.pinned,
                    },
                    **kwargs,
                }
            )

    return FakeMessage(role="system", content=content, pinned=False)


def _build_grep_content(n: int = 500) -> str:
    lines = [
        f"./src/file_{i}.py:42:    return some_function_{i}(args)" for i in range(n)
    ]
    cmd = 'Ran command: `rg -n "pattern" src/`\n'
    return cmd + "\n".join(lines)


def _build_json_array(n: int = 500) -> str:
    items = [
        {
            "id": i,
            "name": f"item_{i}",
            "status": "ok",
            "version": i % 5,
            "description": f"sample entry #{i} with enough text to be realistic",
        }
        for i in range(n)
    ]
    import json

    cmd = "Ran command: `curl https://example.com/api/items/`\n"
    return cmd + json.dumps(items)


def _build_prose_content(n: int = 500) -> str:
    para = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
    )
    cmd = "Ran command: `cat README.md`\n"
    return cmd + para * n


# ── _is_compressible_message tests ─────────────────────────────────


class TestIsCompressibleMessage:
    @pytest.fixture(autouse=True)
    def _import(self):
        from headroom_compressor.hooks.compressor import _is_compressible_message

        self._check = _is_compressible_message

    def test_compressible_grep_output(self):
        msg = _tool_message(_build_grep_content(200))
        assert self._check(msg, min_chars=1)

    def test_compressible_json_array(self):
        msg = _tool_message(_build_json_array(200))
        assert self._check(msg, min_chars=1)

    def test_small_message_not_compressible(self):
        msg = _tool_message("Ran command: `ls`\nsmall")
        assert not self._check(msg, min_chars=5000)

    def test_non_tool_message_not_compressible(self):
        msg = _tool_message("Just some random text without a tool prefix")
        msg.role = "user"
        assert not self._check(msg)

    def test_pinned_message_not_compressible(self):
        msg = _tool_message(_build_grep_content(200))
        msg.pinned = True
        assert not self._check(msg)

    def test_already_compressed_skipped(self):
        from headroom_compressor.hooks.compressor import COMPRESSED_MARKER

        content = COMPRESSED_MARKER + " (orig=1000, strategy=passthrough)]\n..."
        msg = _tool_message(content)
        assert not self._check(msg)

    def test_empty_content_skipped(self):
        msg = _tool_message("")
        assert not self._check(msg)

    def test_below_min_chars(self):
        msg = _tool_message("Ran command: `ls`\n" + "x" * 100)
        assert not self._check(msg, min_chars=500)


# ── generation_pre_hook tests ─────────────────────────────────────


class TestGenerationPreHook:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("GPTME_HEADROOM_ENABLED", "1")

    def test_hook_enabled_flag(self, monkeypatch):
        """Feature flag gates the hook."""
        from headroom_compressor.hooks.compressor import generation_pre_hook

        monkeypatch.setenv("GPTME_HEADROOM_ENABLED", "0")
        msg = _tool_message(_build_grep_content(20))
        msgs = [msg]
        list(generation_pre_hook(msgs))

    def test_hook_does_not_modify_when_disabled(self, monkeypatch):
        """Disabled hook leaves messages unchanged."""
        from headroom_compressor.hooks.compressor import generation_pre_hook

        monkeypatch.delenv("GPTME_HEADROOM_ENABLED", raising=False)
        content = _build_grep_content(200)
        msg = _tool_message(content)
        msgs = [msg]
        list(generation_pre_hook(msgs))
        assert msgs[0].content == content

    def test_hook_passthrough_on_unstructured(self, monkeypatch):
        """Unstructured/prose content is NOT compressed (left for trimmer)."""
        from headroom_compressor.hooks.compressor import generation_pre_hook

        content = _build_prose_content(100)
        msg = _tool_message(content)
        msgs = [msg]
        list(generation_pre_hook(msgs))
        assert "[Headroom compressed" not in msgs[0].content

    def test_small_message_not_compressed(self, monkeypatch):
        """Small messages are not touched."""
        from headroom_compressor.hooks.compressor import generation_pre_hook

        content = "Ran command: `ls`\nsmall"
        msg = _tool_message(content)
        msgs = [msg]
        list(generation_pre_hook(msgs))
        assert msgs[0].content == content

    def test_register(self, monkeypatch):
        """Registration produces a valid hook entry (name, priority)."""
        try:
            from gptme.hooks import registry  # noqa: F401
        except ImportError:
            pytest.skip("gptme version does not expose hooks.registry")

        from gptme.hooks import HookType

        fresh_registry = registry.HookRegistry()
        # Monkeypatch at the re-export level so delayed imports in register() pick it up
        monkeypatch.setattr("gptme.hooks.register_hook", fresh_registry.register)

        from headroom_compressor.hooks.compressor import register

        register()

        hooks = fresh_registry.hooks.get(HookType.GENERATION_PRE, [])
        matching = [h for h in hooks if h.name == "headroom_compressor.generation_pre"]
        assert len(matching) == 1
        assert matching[0].priority == 201
        assert matching[0].enabled is True
