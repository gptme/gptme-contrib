"""Repo-family membership drives the T2c cross-repo review aspects."""

from gptme_review.core import repo_family, select_aspects


def test_placement_prompt_names_no_agent() -> None:
    aspects = select_aspects("", repo="gptme/gptme-contrib")
    prompt = aspects["cross_repo_placement"]
    assert "agent brain repos" in prompt
    assert "another agent's brain repo" in prompt
    for name in ("bob", "alice"):
        assert name not in prompt.lower()


def test_unknown_repo_has_no_family() -> None:
    assert repo_family("someone/unrelated") is None
    assert "cross_repo_placement" not in select_aspects("", repo="someone/unrelated")
