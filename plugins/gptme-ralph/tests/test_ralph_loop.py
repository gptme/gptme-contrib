"""Tests for the Ralph Loop plugin."""

import pytest

from gptme_ralph.tools.ralph_loop import (
    Plan,
    PlanStep,
    create_plan,
    _build_prompt,
)


class TestPlan:
    """Tests for the Plan class."""

    def test_from_markdown_basic(self):
        """Test parsing a basic markdown plan."""
        content = """# My Plan

- [ ] Step 1: Do the first thing
- [ ] Step 2: Do the second thing
- [x] Step 3: Already done
- [ ] Step 4: Final step
"""
        plan = Plan.from_markdown(content)

        assert plan.title == "My Plan"
        assert len(plan.steps) == 4
        assert plan.steps[0].description == "Step 1: Do the first thing"
        assert plan.steps[0].status == "pending"
        assert plan.steps[2].status == "completed"
        assert plan.current_step == 1

    def test_from_markdown_numbered_list(self):
        """Test parsing a numbered list plan."""
        content = """# Implementation Plan

1. First task
2. Second task
3. Third task
"""
        plan = Plan.from_markdown(content)

        assert len(plan.steps) == 3
        assert plan.steps[0].description == "First task"
        assert plan.steps[1].description == "Second task"

    def test_from_markdown_partial_completion(self):
        """Test parsing a partially completed plan."""
        content = """# Test Plan

- [x] Done step 1
- [x] Done step 2
- [ ] Pending step 3
- [ ] Pending step 4
"""
        plan = Plan.from_markdown(content)

        assert plan.current_step == 3
        assert plan.steps[0].status == "completed"
        assert plan.steps[1].status == "completed"
        assert plan.steps[2].status == "pending"

    def test_from_markdown_all_completed(self):
        """Test parsing a fully completed plan."""
        content = """# Complete Plan

- [x] Step 1
- [x] Step 2
"""
        plan = Plan.from_markdown(content)

        assert plan.current_step == 3  # Beyond the last step
        assert plan.get_current_step() is None

    def test_to_markdown_roundtrip(self):
        """Test that to_markdown produces valid markdown."""
        original = """# Test Plan

- [ ] Step 1: First
- [x] Step 2: Second
- [ ] Step 3: Third
"""
        plan = Plan.from_markdown(original)
        output = plan.to_markdown()

        assert "# Test Plan" in output
        assert "- [ ] Step 1: First" in output
        assert "- [x] Step 2: Second" in output

    def test_get_current_step(self):
        """Test getting the current step to work on."""
        content = """# Plan

- [x] Done
- [ ] Current
- [ ] Future
"""
        plan = Plan.from_markdown(content)
        step = plan.get_current_step()

        assert step is not None
        assert step.number == 2
        assert step.description == "Current"

    def test_mark_step_completed(self):
        """Test marking a step as completed."""
        content = """# Plan

- [ ] Step 1
- [ ] Step 2
"""
        plan = Plan.from_markdown(content)

        plan.mark_step_completed(1, notes="Done!")
        assert plan.steps[0].status == "completed"
        assert plan.steps[0].notes == "Done!"
        assert plan.current_step == 2


class TestPlanStep:
    """Tests for the PlanStep class."""

    def test_default_status(self):
        """Test that default status is pending."""
        step = PlanStep(number=1, description="Test step")
        assert step.status == "pending"
        assert step.notes == ""


class TestBuildPrompt:
    """Tests for prompt building."""

    def test_build_prompt_includes_spec(self):
        """Test that the prompt includes the spec."""
        spec = "This is the specification."
        plan = Plan(title="Test", steps=[PlanStep(1, "Step 1")])
        step = plan.steps[0]

        prompt = _build_prompt(spec, plan, step, plan_file_path="test_plan.md")

        assert "This is the specification." in prompt
        assert "Step 1" in prompt
        assert "step 1" in prompt.lower()

    def test_build_prompt_includes_plan(self):
        """Test that the prompt includes the plan."""
        spec = "Spec"
        plan = Plan(
            title="My Plan",
            steps=[
                PlanStep(1, "First step", status="completed"),
                PlanStep(2, "Second step"),
            ],
        )
        step = plan.steps[1]

        prompt = _build_prompt(spec, plan, step, plan_file_path="plan.md")

        assert "My Plan" in prompt
        assert "First step" in prompt
        assert "Second step" in prompt

    def test_build_prompt_includes_plan_file_path(self):
        """Test that the prompt includes the plan file path for checkbox updates."""
        spec = "Test spec"
        plan = Plan(title="Test Plan", steps=[PlanStep(1, "Step 1")])
        step = plan.steps[0]

        prompt = _build_prompt(spec, plan, step, plan_file_path="/workspace/plan.md")

        # Verify the plan file path is explicitly mentioned
        assert "/workspace/plan.md" in prompt
        # Verify checkbox update pattern is explained
        assert "- [ ]" in prompt or "[ ]" in prompt
        assert "- [x]" in prompt or "[x]" in prompt


class TestCreatePlan:
    """Tests for plan creation."""

    def test_create_plan_creates_file(self, tmp_path):
        """Test that create_plan creates a file."""
        result = create_plan(
            "Build a test feature",
            output_file="test_plan.md",
            workspace=str(tmp_path),
        )

        plan_path = tmp_path / "test_plan.md"
        assert plan_path.exists()
        assert "Build a test feature" in plan_path.read_text()
        assert "test_plan.md" in result

    def test_create_plan_includes_checkboxes(self, tmp_path):
        """Test that created plan has checkbox format."""
        create_plan(
            "Test task",
            output_file="plan.md",
            workspace=str(tmp_path),
        )

        content = (tmp_path / "plan.md").read_text()
        assert "- [ ]" in content
        assert "Step" in content


class TestIntegration:
    """Integration tests (require backend to be available)."""

    @pytest.mark.skip(reason="Requires claude or gptme CLI")
    def test_run_loop_simple(self, tmp_path):
        """Test running a simple loop."""
        # Create spec
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("Build a hello world script.")

        # Create plan
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            """# Plan

- [ ] Create hello.py
- [ ] Test it works
"""
        )

        # This would require the actual backend
        # from gptme_ralph.tools.ralph_loop import run_loop
        # result = run_loop("spec.md", "plan.md", workspace=str(tmp_path))
