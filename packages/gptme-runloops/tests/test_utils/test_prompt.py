"""Tests for prompt generation utilities."""

import pytest
from gptme_runloops.utils.prompt import (
    generate_base_prompt,
    get_agent_name,
    read_prompt_template,
)


class TestGetAgentName:
    def test_returns_name_from_toml(self, tmp_path):
        """Reads agent name from gptme.toml when present."""
        (tmp_path / "gptme.toml").write_text('[agent]\nname = "Alice"\n')
        assert get_agent_name(tmp_path) == "Alice"

    def test_falls_back_when_no_toml(self, tmp_path):
        """Returns 'Agent' when gptme.toml is missing."""
        assert get_agent_name(tmp_path) == "Agent"

    def test_falls_back_when_no_agent_section(self, tmp_path):
        """Returns 'Agent' when gptme.toml has no [agent] table."""
        (tmp_path / "gptme.toml").write_text("[prompt]\nfiles = []\n")
        assert get_agent_name(tmp_path) == "Agent"

    def test_falls_back_when_agent_section_has_no_name(self, tmp_path):
        """Returns 'Agent' when [agent] table has no 'name' key."""
        (tmp_path / "gptme.toml").write_text('[agent]\nmodel = "sonnet"\n')
        assert get_agent_name(tmp_path) == "Agent"

    def test_falls_back_on_invalid_toml(self, tmp_path):
        """Returns 'Agent' when gptme.toml contains invalid TOML."""
        (tmp_path / "gptme.toml").write_text("this is: not valid [[TOML\n")
        assert get_agent_name(tmp_path) == "Agent"

    def test_ignores_non_string_name(self, tmp_path):
        """Returns 'Agent' when [agent].name is not a string (e.g. integer)."""
        (tmp_path / "gptme.toml").write_text("[agent]\nname = 42\n")
        assert get_agent_name(tmp_path) == "Agent"


class TestGenerateBasePrompt:
    def test_contains_agent_name(self):
        """Generated prompt mentions the agent name."""
        prompt = generate_base_prompt("autonomous", agent_name="Bob")
        assert "Bob" in prompt

    def test_contains_run_type(self):
        """Generated prompt mentions the run type."""
        prompt = generate_base_prompt("email", agent_name="Bob")
        assert "email" in prompt

    def test_uses_provided_time(self):
        """When current_time is supplied, it appears in the prompt."""
        ts = "2026-06-11T12:00:00+00:00"
        prompt = generate_base_prompt("autonomous", current_time=ts)
        assert ts in prompt

    def test_uses_default_time_when_none(self):
        """When current_time is None, the prompt still contains a timestamp."""
        prompt = generate_base_prompt("autonomous")
        assert "Current Time" in prompt

    def test_context_budget_formatted(self):
        """context_budget appears formatted with commas."""
        prompt = generate_base_prompt("autonomous", context_budget=200_000)
        assert "200,000" in prompt

    def test_additional_sections_appended(self):
        """Additional sections are appended verbatim."""
        extra = "## Special Instructions\nDo the thing."
        prompt = generate_base_prompt("autonomous", additional_sections=extra)
        assert "Special Instructions" in prompt
        assert "Do the thing." in prompt

    def test_no_additional_sections_by_default(self):
        """Default prompt doesn't include a placeholder for extra sections."""
        prompt = generate_base_prompt("autonomous")
        assert "Special Instructions" not in prompt

    def test_returns_string(self):
        prompt = generate_base_prompt("monitoring")
        assert isinstance(prompt, str)
        assert len(prompt) > 0


class TestReadPromptTemplate:
    def test_reads_file_content(self, tmp_path):
        """Returns the exact content of the template file."""
        template = tmp_path / "prompt.md"
        template.write_text("Hello, {agent_name}!\n")
        assert read_prompt_template(template) == "Hello, {agent_name}!\n"

    def test_raises_on_missing_file(self, tmp_path):
        """Raises FileNotFoundError for a non-existent template."""
        with pytest.raises(FileNotFoundError):
            read_prompt_template(tmp_path / "nonexistent.md")

    def test_reads_multiline_template(self, tmp_path):
        """Multi-line templates are returned intact."""
        content = "Line 1\nLine 2\nLine 3\n"
        template = tmp_path / "multi.md"
        template.write_text(content)
        assert read_prompt_template(template) == content
