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


def _build_cat_content(n: int = 500) -> str:
    """Tool output from a 'cat' command, used to test raw_tool_prefixes."""
    lines = [f"line {i}: some config content here" for i in range(n)]
    cmd = "Ran command: `cat /etc/config.ini`\n"
    return cmd + "\n".join(lines)


# ── _coerce_raw_tool_prefixes tests ────────────────────────────────


class TestCoerceRawToolPrefixes:
    @pytest.fixture(autouse=True)
    def _import(self):
        from headroom_compressor.hooks.compressor import _coerce_raw_tool_prefixes

        self._coerce = _coerce_raw_tool_prefixes

    def test_none(self):
        assert self._coerce(None) == ()

    def test_string(self):
        assert self._coerce("cat ") == ("cat ",)

    def test_list(self):
        assert self._coerce(["cat ", "echo "]) == ("cat ", "echo ")

    def test_tuple(self):
        assert self._coerce(("cat ",)) == ("cat ",)

    def test_invalid_type(self):
        assert self._coerce(42) == ()

    def test_mixed_list_skips_non_strings(self):
        assert self._coerce(["cat ", 42, "echo "]) == ("cat ", "echo ")


# ── _content_matches_raw_prefix tests ──────────────────────────────


class TestContentMatchesRawPrefix:
    @pytest.fixture(autouse=True)
    def _import(self):
        from headroom_compressor.hooks.compressor import _content_matches_raw_prefix

        self._match = _content_matches_raw_prefix

    def test_matches_cat_prefix(self):
        content = "Ran command: `cat /etc/config.ini`\nfoo\nbar\n"
        assert self._match(content, ("cat ",))

    def test_matches_echo_prefix(self):
        content = 'Ran command: `echo "hello"`\nhello\n'
        assert self._match(content, ("echo",))

    def test_no_match_without_prefixes(self):
        content = "Ran command: `cat /etc/config.ini`\nfoo\n"
        assert not self._match(content, ())

    def test_no_match_for_different_prefix(self):
        content = "Ran command: `grep pattern file.txt`\nresult\n"
        assert not self._match(content, ("cat ",))

    def test_no_match_for_executed_code_block(self):
        """Executed code blocks don't have a command to match against."""
        content = "Executed code block.\nfoo\nbar\n"
        assert not self._match(content, ("cat ",))

    def test_empty_content(self):
        assert not self._match("", ("cat ",))


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

    def test_skipped_by_raw_prefix(self):
        """Messages matching raw_tool_prefixes should not be compressed."""
        msg = _tool_message(_build_cat_content(200))
        assert not self._check(msg, min_chars=1, raw_prefixes=("cat ",))

    def test_not_skipped_by_unrelated_prefix(self):
        """Messages not matching raw_tool_prefixes should still be compressible."""
        msg = _tool_message(_build_grep_content(200))
        assert self._check(msg, min_chars=1, raw_prefixes=("cat ",))


# ── get_compressor_config tests ─────────────────────────────────────


class TestGetCompressorConfig:
    @pytest.fixture(autouse=True)
    def _import(self):
        from headroom_compressor.hooks.compressor import get_compressor_config

        self._get_config = get_compressor_config

    def test_default_config_disabled(self):
        """No env var, no config file → disabled."""
        cfg = self._get_config()
        assert not cfg.enabled
        assert cfg.min_compress_chars == 2000
        assert cfg.raw_tool_prefixes == ()

    def test_env_var_enables(self):
        """GPTME_HEADROOM_ENABLED=1 → enabled."""
        import os

        os.environ["GPTME_HEADROOM_ENABLED"] = "1"
        try:
            cfg = self._get_config()
            assert cfg.enabled
        finally:
            del os.environ["GPTME_HEADROOM_ENABLED"]

    def test_env_var_false(self):
        """GPTME_HEADROOM_ENABLED=0 → disabled."""
        import os

        os.environ["GPTME_HEADROOM_ENABLED"] = "0"
        try:
            cfg = self._get_config()
            assert not cfg.enabled
        finally:
            del os.environ["GPTME_HEADROOM_ENABLED"]

    def test_file_empty_settings_stays_disabled(self):
        """Config file with no plugin section → disabled (but doesn't crash)."""
        cfg = self._get_config()
        # Should not crash; default is disabled
        assert not cfg.enabled


# ── generation_pre_hook tests ──────────────────────────────────────


class TestGenerationPreHook:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("GPTME_HEADROOM_ENABLED", "1")

    def test_get_crusher_cached_import_failure_returns_none(self, monkeypatch):
        """Cached import failure sentinel does not get called as a class."""
        from headroom_compressor.hooks import compressor

        monkeypatch.setattr(compressor, "_SmartCrusher", False)
        assert compressor._get_crusher() is None

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

    def test_hook_compresses_structured_tool_output(self, monkeypatch):
        """Structured tool output is compressed and keeps its command prefix."""
        from dataclasses import dataclass

        from headroom_compressor.hooks import compressor

        @dataclass
        class FakeResult:
            was_modified: bool = True
            strategy: str = "test"
            compressed: str = '{"items":"compressed"}'

        class FakeCrusher:
            def crush(self, data: str) -> FakeResult:
                assert data.startswith("[")
                assert not data.startswith("Ran command:")
                return FakeResult()

        monkeypatch.setattr(compressor, "_get_crusher", lambda: FakeCrusher())
        content = _build_json_array(200)
        msg = _tool_message(content)
        msgs = [msg]

        list(compressor.generation_pre_hook(msgs))

        assert msgs[0].content.startswith("[Headroom compressed")
        assert "strategy=test" in msgs[0].content
        assert "Ran command: `curl https://example.com/api/items/`" in msgs[0].content
        assert '{"items":"compressed"}' in msgs[0].content

    def test_hook_skips_raw_prefix_messages(self, monkeypatch):
        """Messages matching raw_tool_prefixes are NOT compressed."""
        from headroom_compressor.hooks import compressor

        # Mock get_compressor_config to return a config with raw prefixes

        def patched_config():
            return compressor.HeadroomCompressorConfig(
                enabled=True,
                min_compress_chars=1,
                raw_tool_prefixes=("cat ",),
            )

        monkeypatch.setattr(compressor, "get_compressor_config", patched_config)

        content = _build_cat_content(200)
        msg = _tool_message(content)
        msgs = [msg]
        list(compressor.generation_pre_hook(msgs))
        # Should not be compressed — cat output matches raw prefix
        assert "[Headroom compressed" not in msgs[0].content

    def test_hook_compresses_non_raw_prefix(self, monkeypatch):
        """Messages NOT matching raw_tool_prefixes ARE compressed by fake crusher."""
        from dataclasses import dataclass

        from headroom_compressor.hooks import compressor

        @dataclass
        class FakeResult:
            was_modified: bool = True
            strategy: str = "json"
            compressed: str = "[compressed]"

        class FakeCrusher:
            def crush(self, data: str) -> FakeResult:
                return FakeResult()

        monkeypatch.setattr(compressor, "_get_crusher", lambda: FakeCrusher())

        def patched_config():
            return compressor.HeadroomCompressorConfig(
                enabled=True,
                min_compress_chars=1,
                raw_tool_prefixes=("cat ",),
            )

        monkeypatch.setattr(compressor, "get_compressor_config", patched_config)

        content = _build_grep_content(200)
        msg = _tool_message(content)
        msgs = [msg]
        list(compressor.generation_pre_hook(msgs))
        # Should be compressed — grep output doesn't match "cat " prefix
        assert "[Headroom compressed" in msgs[0].content

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
        monkeypatch.setattr("gptme.hooks.register_hook", fresh_registry.register)

        from headroom_compressor.hooks.compressor import register

        register()

        hooks = fresh_registry.hooks.get(HookType.GENERATION_PRE, [])
        matching = [h for h in hooks if h.name == "headroom_compressor.generation_pre"]
        assert len(matching) == 1
        assert matching[0].priority == 201
        assert matching[0].enabled is True
