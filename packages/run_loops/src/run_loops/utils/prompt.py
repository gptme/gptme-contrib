"""Prompt generation utilities for run loops."""

from datetime import datetime
from pathlib import Path
from typing import Optional


def generate_base_prompt(
    run_type: str,
    agent_name: str = "Agent",
    current_time: Optional[str] = None,
    context_budget: int = 200000,
    additional_sections: Optional[str] = None,
) -> str:
    """Generate base prompt template for run loops.

    Args:
        run_type: Type of run (autonomous, email, monitoring)
        agent_name: Name of the agent running the loop
        current_time: ISO formatted time (defaults to now)
        context_budget: Token budget for the run
        additional_sections: Additional prompt sections to append

    Returns:
        Generated prompt text
    """
    if current_time is None:
        current_time = datetime.now().astimezone().isoformat()

    prompt = f"""You are {agent_name}, running in {run_type} mode.

**Current Time**: {current_time}
**Context Budget**: {context_budget:,} tokens

"""

    if additional_sections:
        prompt += additional_sections

    return prompt


def read_prompt_template(template_file: Path) -> str:
    """Read prompt template from file.

    Args:
        template_file: Path to template file

    Returns:
        Template content
    """
    return template_file.read_text()
