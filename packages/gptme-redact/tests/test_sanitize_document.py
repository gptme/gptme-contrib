"""Tests for the Markdown document-conversion safety scanner.

Coverage, in order of safety importance:

1. Formula injection -- CRITICAL -- the canonical CSV/spreadsheet payload
   class. Detected at line-start AND inside Markdown table cells. Benign
   arithmetic and Markdown list markers must NOT trigger.
2. HTML / script injection -- HIGH -- the second most common carry-over
   from Word/PDF converters. Covers ``<script>``, ``javascript:`` hrefs,
   inline event handlers, and dangerous HTML staging tags.
3. Suspicious metadata comments -- MEDIUM -- author/revision/path leakage
   in HTML comments that survive Markdown conversion.
4. Default block_severity gate -- HIGH and above flip ``blocked``; callers
   can lower to CRITICAL (formula-only) or raise to LOW.
"""

from __future__ import annotations

import json

from redact import Severity
from redact.sanitize_document import (
    DocumentScanResult,
    scan_document,
)


# --- Formula injection -------------------------------------------------------
class TestFormulaInjection:
    """CSV injection / CWE-1236: spreadsheet formula trigger at line or cell start."""

    def test_detects_formula_in_table_cell(self):
        """The firecrawl / anydoc output form: a Markdown table from Excel."""
        text = "| =cmd|'/c calc'!A0 | foo |\n| bar | baz |\n"
        r = scan_document(text)
        cats = [f.category for f in r.findings]
        assert "formula_injection" in cats
        assert r.blocked  # CRITICAL >= default HIGH
        f = next(f for f in r.findings if f.category == "formula_injection")
        assert f.severity == Severity.CRITICAL
        assert f.count == 1

    def test_detects_formula_at_line_start(self):
        text = "Some intro\n=cmd|/c calc!A0\nmore text\n"
        r = scan_document(text)
        assert any(f.category == "formula_injection" for f in r.findings), r.findings

    def test_detects_at_sign_trigger(self):
        """``@SUM(...)`` is the Excel/LibreOffice function-call prefix."""
        text = "Q1 totals\n@SUM(A1:A10)\nNotes\n"
        r = scan_document(text)
        assert any(f.category == "formula_injection" for f in r.findings), r.findings

    def test_detects_plus_minus_trigger(self):
        """``+`` and ``-`` are formula triggers (legacy Lotus 1-2-3) at line/cell start."""
        # Triggers must be at line start or after a table pipe; inline arithmetic
        # like "Cell A1: +1+1" is NOT a formula trigger (see test_does_not_trigger_*)
        text = "+1+1\n-2+3\n"
        r = scan_document(text)
        assert any(f.category == "formula_injection" for f in r.findings), r.findings

    def test_does_not_trigger_on_arithmetic(self):
        """Inline arithmetic like ``- 5 + 3`` is benign prose."""
        text = "Math: - 5 + 3 = -2\nResult: +5 units\n"
        r = scan_document(text)
        assert (
            r.findings == []
        ), f"arithmetic prose should not trigger formula_injection, got {r.findings}"

    def test_does_not_trigger_on_markdown_list(self):
        """Markdown list items ``- foo`` are not formula triggers."""
        text = "- item one\n- item two\n- item three\n"
        r = scan_document(text)
        assert r.findings == [], f"markdown list should not trigger, got {r.findings}"

    def test_does_not_trigger_on_equals_in_text(self):
        """A bare ``=`` sign in prose (e.g. ``x = 5``) is not injection."""
        text = "We have x = 5 and y = 10, so x + y = 15.\n"
        r = scan_document(text)
        assert r.findings == [], f"prose equation should not trigger, got {r.findings}"

    def test_multiple_formulas_counted(self):
        text = (
            "Table from Excel:\n"
            "| =cmd|'calc'!A0 | foo |\n"
            "| @SUM(A:A) | bar |\n"
            "| +1+9 | baz |\n"
            "\nMore:\n"
            "- note one\n"
        )
        r = scan_document(text)
        f = next(f_ for f_ in r.findings if f_.category == "formula_injection")
        # 3 formula triggers in cells; the leading "- note one" prose
        # line does NOT trigger.
        assert f.count == 3


# --- HTML / script injection -------------------------------------------------
class TestHtmlScriptInjection:
    """Markdown passthrough HTML / event handlers / dangerous URI schemes."""

    def test_detects_script_tag(self):
        text = "Body text\n<script>alert(1)</script>\n"
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings
        assert r.blocked

    def test_detects_iframe_tag(self):
        text = '<iframe src="https://evil.example.com"></iframe>\n'
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_javascript_in_markdown_link(self):
        """Markdown link ``[text](javascript:...)`` is the firecrawl output form."""
        text = "[click](javascript:alert(1))\n"
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_javascript_in_html_attribute(self):
        text = '<a href="javascript:alert(1)">click</a>\n'
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_vbscript_scheme(self):
        text = "<a href='vbscript:msgbox(1)'>x</a>\n"
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_onerror_handler(self):
        text = "<img src=x onerror=alert(1)>\n"
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_onclick_handler(self):
        text = '<button onclick="evil()">Click</button>\n'
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_detects_dangerous_meta_tag(self):
        """``<meta http-equiv=refresh>`` is a redirect hijack vector."""
        text = '<meta http-equiv="refresh" content="0;url=https://evil">\n'
        r = scan_document(text)
        assert any(f.category == "html_script" for f in r.findings), r.findings

    def test_does_not_trigger_on_safe_markdown_link(self):
        """Normal Markdown links to https URLs are safe."""
        text = (
            "[example](https://example.com)\n"
            "[github](https://github.com/x)\n"
            "[empty](https://x.org/path?q=1)\n"
        )
        r = scan_document(text)
        assert (
            r.findings == []
        ), f"safe markdown links should not trigger, got {r.findings}"


# --- Suspicious metadata -----------------------------------------------------
class TestMetadataLeak:
    """HTML comments carrying author / revision / path information."""

    def test_detects_author_metadata(self):
        text = "Body\n<!-- author: jane.doe@corp.example -->\nMore\n"
        r = scan_document(text)
        assert any(f.category == "metadata_leak" for f in r.findings), r.findings
        f = next(f_ for f_ in r.findings if f_.category == "metadata_leak")
        assert f.severity == Severity.MEDIUM

    def test_detects_revision_metadata(self):
        text = "<!-- Revision: 17, last modified 2024-01-01 by admin -->\n"
        r = scan_document(text)
        assert any(f.category == "metadata_leak" for f in r.findings), r.findings

    def test_detects_filename_metadata(self):
        text = "<!-- filename: quarterly-report.docx, owner: finance -->\n"
        r = scan_document(text)
        assert any(f.category == "metadata_leak" for f in r.findings), r.findings

    def test_does_not_trigger_on_plain_comment(self):
        """HTML comments without leak keywords are not flagged."""
        text = (
            "<!-- TODO: refactor this section -->\n"
            "<!-- note: see also the appendix -->\n"
        )
        r = scan_document(text)
        assert (
            r.findings == []
        ), f"plain HTML comments should not trigger, got {r.findings}"

    def test_counts_multiple_metadata_comments(self):
        text = (
            "<!-- author: a -->\n"
            "<!-- revision: 2 -->\n"
            "<!-- TODO: nothing -->\n"
            "<!-- filename: x -->\n"
        )
        r = scan_document(text)
        f = next(f_ for f_ in r.findings if f_.category == "metadata_leak")
        assert f.count == 3  # TODO comment is benign


# --- Severity gating and ordering --------------------------------------------
class TestSeverityGate:
    """Default block_severity, ordering, and caller's threshold overrides."""

    def test_default_blocks_at_high_or_above(self):
        """``block_severity`` defaults to HIGH: formula + HTML block."""
        text = "Some prose\n| =cmd|'calc'!A0 | foo |\n<script>alert(1)</script>\n"
        r = scan_document(text)
        assert r.blocked

    def test_critical_threshold_only_blocks_formula(self):
        """CRITICAL threshold: only formula triggers the block flag."""
        text = "<script>alert(1)</script>\n"
        r = scan_document(text, block_severity=Severity.CRITICAL)
        assert any(f.category == "html_script" for f in r.findings)
        assert not r.blocked  # HIGH < CRITICAL

    def test_low_threshold_blocks_even_metadata(self):
        """LOW threshold: even MEDIUM metadata flips blocked."""
        text = "<!-- author: x -->\n"
        r = scan_document(text, block_severity=Severity.LOW)
        assert any(f.category == "metadata_leak" for f in r.findings)
        assert r.blocked

    def test_findings_ordered_by_severity_descending(self):
        """Formula (CRITICAL) > HTML (HIGH) > metadata (MEDIUM)."""
        text = "<!-- author: x -->\n<script>alert(1)</script>\n| =cmd|'x'!A0 | foo |\n"
        r = scan_document(text)
        severities = [int(f.severity) for f in r.findings]
        assert severities == sorted(
            severities, reverse=True
        ), f"findings should be severity-descending, got {severities}"
        assert [f.category for f in r.findings] == [
            "formula_injection",
            "html_script",
            "metadata_leak",
        ]

    def test_clean_document_returns_empty(self):
        text = (
            "# Title\n\n"
            "Some prose about quarterly results.\n\n"
            "- item one\n"
            "- item two\n\n"
            "[ok](https://example.com)\n"
        )
        r = scan_document(text)
        assert r.findings == []
        assert not r.blocked
        assert r.input_chars == len(text)


# --- Result envelope ---------------------------------------------------------
class TestDocumentScanResult:
    """Typed result shape and JSON serialization."""

    def test_to_dict_shape(self):
        text = "| =cmd|'x'!A0 | foo |\n"
        r = scan_document(text)
        d = r.to_dict()
        assert d["blocked"] is True
        assert d["input_chars"] == len(text)
        assert isinstance(d["findings"], list)
        f = d["findings"][0]
        assert set(f.keys()) == {"category", "severity", "count", "preview"}

    def test_to_dict_serializes_to_json(self):
        text = "| =cmd|'x'!A0 | foo |\n"
        r = scan_document(text)
        # Round-trip via json.loads to confirm serializable.
        parsed = json.loads(json.dumps(r.to_dict()))
        assert parsed["blocked"] is True
        assert parsed["findings"][0]["category"] == "formula_injection"

    def test_empty_input(self):
        r = scan_document("")
        assert r.findings == []
        assert r.input_chars == 0
        assert not r.blocked
        assert isinstance(r, DocumentScanResult)


# --- Regression: existing tests still pass ----------------------------------
def test_does_not_modify_input():
    """Scanner is detection-only; text must not be mutated."""
    text = "| =cmd|'calc'!A0 | foo |\n<script>alert(1)</script>\n"
    snapshot = text
    scan_document(text)
    assert text == snapshot
