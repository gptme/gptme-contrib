"""Tests for warp-grep plugin."""

from gptme_warp_grep.tools.warp_grep import (
    LocalProvider,
    format_analyse_tree,
    format_tool_result,
    format_turn_message,
    parse_tool_calls,
)


class TestParseToolCalls:
    """Tests for parsing tool calls from model output."""

    def test_parse_grep(self):
        text = "<tool_call>grep 'authenticate' src/</tool_call>"
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].arguments == {"pattern": "authenticate", "path": "src/"}

    def test_parse_read_with_range(self):
        text = "<tool_call>read src/auth.py:10-50</tool_call>"
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "read"
        assert calls[0].arguments == {"path": "src/auth.py", "start": 10, "end": 50}

    def test_parse_read_without_range(self):
        text = "<tool_call>read src/main.py</tool_call>"
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "read"
        assert calls[0].arguments == {"path": "src/main.py"}

    def test_parse_analyse(self):
        text = "<tool_call>analyse src/api</tool_call>"
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "analyse"
        assert calls[0].arguments == {"path": "src/api"}

    def test_parse_analyse_with_pattern(self):
        text = '<tool_call>analyse . ".*\\.py$"</tool_call>'
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "analyse"
        assert calls[0].arguments == {"path": ".", "pattern": ".*\\.py$"}

    def test_parse_finish(self):
        text = "<tool_call>finish src/auth.py:1-15,25-50 src/types.py:1-30</tool_call>"
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "finish"
        files = calls[0].arguments["files"]
        assert len(files) == 2
        assert files[0] == {"path": "src/auth.py", "lines": [[1, 15], [25, 50]]}
        assert files[1] == {"path": "src/types.py", "lines": [[1, 30]]}

    def test_parse_multiple_calls(self):
        text = """<think>
This is my reasoning.
</think>
<tool_call>grep 'auth' src/</tool_call>
<tool_call>grep 'login' src/</tool_call>
<tool_call>analyse src/api</tool_call>"""
        calls = parse_tool_calls(text)
        assert len(calls) == 3
        assert calls[0].name == "grep"
        assert calls[1].name == "grep"
        assert calls[2].name == "analyse"

    def test_removes_think_blocks(self):
        text = """<think>
Long reasoning here.
Multiple lines.
</think>
<tool_call>grep 'test' .</tool_call>"""
        calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "grep"


class TestFormatFunctions:
    """Tests for formatting functions."""

    def test_format_tool_result(self):
        result = format_tool_result(
            "grep", {"pattern": "auth", "path": "src/"}, "src/auth.py:10:def auth():"
        )
        assert '<grep_output pattern="auth" path="src/">' in result
        assert "src/auth.py:10:def auth():" in result
        assert "</grep_output>" in result

    def test_format_turn_message_early(self):
        msg = format_turn_message(1, 4)
        assert "[Turn 1/4]" in msg
        assert "3 turns remaining" in msg

    def test_format_turn_message_last(self):
        msg = format_turn_message(4, 4)
        assert "[Turn 4/4]" in msg
        assert "LAST turn" in msg
        assert "MUST call finish" in msg

    def test_format_analyse_tree(self):
        entries = [
            {"name": "src", "path": "src", "type": "dir", "depth": 0},
            {"name": "main.py", "path": "src/main.py", "type": "file", "depth": 1},
            {"name": "utils", "path": "src/utils", "type": "dir", "depth": 1},
        ]
        result = format_analyse_tree(entries)
        assert "- [D] src" in result
        assert "  - [F] main.py" in result
        assert "  - [D] utils" in result


class TestLocalProvider:
    """Tests for LocalProvider."""

    def test_grep_nonexistent_path(self, tmp_path):
        provider = LocalProvider(tmp_path)
        result = provider.grep("pattern", "nonexistent")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_read_nonexistent_file(self, tmp_path):
        provider = LocalProvider(tmp_path)
        result = provider.read("nonexistent.py")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_read_file(self, tmp_path):
        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("line1\nline2\nline3\n")

        provider = LocalProvider(tmp_path)
        result = provider.read("test.py")
        assert "lines" in result
        assert len(result["lines"]) == 3
        assert "1|line1" in result["lines"][0]

    def test_read_file_with_range(self, tmp_path):
        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        provider = LocalProvider(tmp_path)
        result = provider.read("test.py", start=2, end=4)
        assert "lines" in result
        assert len(result["lines"]) == 3
        assert "2|line2" in result["lines"][0]
        assert "4|line4" in result["lines"][2]

    def test_analyse_directory(self, tmp_path):
        # Create test structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("# main")
        (tmp_path / "tests").mkdir()
        (tmp_path / "README.md").write_text("# readme")

        provider = LocalProvider(tmp_path)
        entries = provider.analyse(".")

        names = [e["name"] for e in entries]
        assert "src" in names
        assert "tests" in names
        assert "README.md" in names
