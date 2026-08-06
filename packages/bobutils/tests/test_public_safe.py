"""Tests for bobutils.public_safe."""

from __future__ import annotations

from bobutils.public_safe import PublicSafeViolation, public_safe, validate_public_safe

# ---------------------------------------------------------------------------
# public_safe() — substitution tests
# ---------------------------------------------------------------------------


def test_workspace_path_replaced() -> None:
    text = "See /home/bob/bob/journal/2026-08-06/session.md for details."
    result = public_safe(text)
    assert "/home/bob/bob/" not in result
    assert "<workspace>/journal/" in result


def test_home_path_replaced() -> None:
    text = "Config at /home/bob/.config/claude/"
    result = public_safe(text)
    assert "/home/bob/" not in result
    assert "<home>/" in result


def test_workspace_path_takes_priority_over_home_path() -> None:
    """More-specific workspace pattern must win over the bare home pattern."""
    text = "/home/bob/bob/packages/bobutils/"
    result = public_safe(text)
    assert "<workspace>/packages/bobutils/" == result


def test_internal_url_replaced() -> None:
    text = "Visit http://bob.hassel.bjareho.lt:8812/decisions/"
    result = public_safe(text)
    assert "bjareho.lt" not in result
    assert "<internal-endpoint>" in result


def test_internal_hostname_replaced() -> None:
    text = "Dashboard is at bob.hassel.bjareho.lt"
    result = public_safe(text)
    assert "bjareho.lt" not in result
    assert "<internal-host>" in result


def test_ssh_remote_replaced() -> None:
    text = "ssh erb-hetzner-ax41 hostname"
    result = public_safe(text)
    assert "erb-hetzner-ax41" not in result
    assert "<ssh-remote>" in result


def test_cluster_node_replaced() -> None:
    text = "cluster1-node1 has 24 cores and cluster1 is the master."
    result = public_safe(text)
    assert "cluster1" not in result
    assert "<cluster-node>" in result


def test_lxc_container_replaced() -> None:
    text = "Container CT102 is running Alice's workspace."
    result = public_safe(text)
    assert "CT102" not in result
    assert "<lxc-container>" in result


def test_pct_exec_replaced() -> None:
    text = "Run: pct exec 102 -- bash"
    result = public_safe(text)
    assert "pct exec 102" not in result
    assert "pct exec <N>" in result


def test_safe_text_unchanged() -> None:
    text = "Shipped BM25 z-score gate for lesson injection in gptme. PR #3453 is open."
    assert public_safe(text) == text


def test_relative_workspace_path_unchanged() -> None:
    """Relative paths are fine in public content."""
    text = "See packages/bobutils/src/bobutils/public_safe.py"
    assert public_safe(text) == text


def test_github_urls_unchanged() -> None:
    text = "https://github.com/gptme/gptme/pull/3453"
    assert public_safe(text) == text


def test_public_domain_unchanged() -> None:
    text = "Website: https://timetobuildbob.com"
    assert public_safe(text) == text


def test_multiple_private_patterns_in_one_string() -> None:
    text = "From /home/bob/bob/ via erb-hetzner-ax41 to bob.hassel.bjareho.lt"
    result = public_safe(text)
    assert "/home/bob/" not in result
    assert "erb-hetzner-ax41" not in result
    assert "bjareho.lt" not in result


def test_mixed_case_hostname_replaced() -> None:
    text = "Dashboard is at Bob.Hassel.Bjareho.lt"
    result = public_safe(text)
    assert "bjareho.lt" not in result.lower()
    assert "<internal-host>" in result


def test_mixed_case_endpoint_replaced() -> None:
    text = "Visit HTTP://Bob.Hassel.Bjareho.lt:8812/decisions/"
    result = public_safe(text)
    assert "bjareho.lt" not in result.lower()
    assert "<internal-endpoint>" in result


def test_endpoint_trailing_punctuation_preserved() -> None:
    text = "See (http://bob.hassel.bjareho.lt:8812/decisions/)."
    result = public_safe(text)
    assert result == "See (<internal-endpoint>)."


def test_endpoint_in_markdown_link_preserved() -> None:
    text = "[dashboard](http://bob.hassel.bjareho.lt:8812/decisions/) is live."
    result = public_safe(text)
    assert result == "[dashboard](<internal-endpoint>) is live."


def test_endpoint_url_with_balanced_parens_kept() -> None:
    """A closing ) that is balanced by an earlier ( inside the URL stays."""
    text = "Archive: http://bob.hassel.bjareho.lt/path/(archive) is available."
    result = public_safe(text)
    assert result == "Archive: <internal-endpoint> is available."


def test_idempotent() -> None:
    """Applying public_safe twice must yield the same result as once."""
    text = "See /home/bob/bob/journal/ at bob.hassel.bjareho.lt:8812"
    once = public_safe(text)
    twice = public_safe(once)
    assert once == twice


# ---------------------------------------------------------------------------
# validate_public_safe() — audit tests
# ---------------------------------------------------------------------------


def test_validate_workspace_path_violation() -> None:
    violations = validate_public_safe("/home/bob/bob/tasks/foo.md")
    kinds = {v.kind for v in violations}
    assert "workspace absolute path" in kinds


def test_validate_internal_hostname_violation() -> None:
    violations = validate_public_safe("host is hassel.bjareho.lt")
    assert any(v.kind == "internal hostname" for v in violations)


def test_validate_clean_text_returns_empty() -> None:
    violations = validate_public_safe("This is safe to publish on Twitter.")
    assert violations == []


def test_validate_returns_public_safe_violation_instances() -> None:
    violations = validate_public_safe("/home/bob/bob/")
    assert all(isinstance(v, PublicSafeViolation) for v in violations)


def test_validate_records_offset() -> None:
    text = "start /home/bob/bob/end"
    violations = validate_public_safe(text)
    assert any(v.offset == text.index("/home/bob/bob/") for v in violations)
