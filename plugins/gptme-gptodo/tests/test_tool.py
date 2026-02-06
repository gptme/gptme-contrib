"""Tests for gptodo tool plugin."""

from gptme_gptodo.tools.gptodo_tool import tool


def test_tool_spec():
    """Test that tool spec is properly defined."""
    assert tool.name == "gptodo"
    assert tool.desc
    assert tool.instructions
    assert tool.functions
    assert len(tool.functions) == 6


def test_functions_registered():
    """Test that all expected functions are registered."""
    func_names = {f.__name__ for f in tool.functions}
    expected = {
        "delegate",
        "check_agent",
        "list_agents",
        "list_tasks",
        "task_status",
        "add_task",
    }
    assert func_names == expected
