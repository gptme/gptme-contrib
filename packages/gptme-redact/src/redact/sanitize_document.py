"""Document-conversion safety scanner for Markdown artifacts.

Validates firecrawl / anydoc-converted Markdown documents for attack vectors
that survive the Word / PDF / Excel -> Markdown pipeline before agents
consume them. Three classes of harm survive naive conversion to Markdown:

1. **Formula injection** -- spreadsheet cells like ``=cmd|'/c calc'!A0``
   render as plain text in Markdown but get interpreted as code if ever
   re-imported into a spreadsheet or processed by a formula-aware tool.
   The canonical CSV injection class (CWE-1236).
2. **HTML / script injection** -- ``<script>alert(1)</script>``,
   ``<img onerror=...>``, ``javascript:`` href attributes that survive a
   Markdown converter's ``passthrough`` mode or incomplete sanitization.
3. **Suspicious metadata comments** -- author names, file paths, revision
   comments, tracked changes embedded as Markdown text by the converter.

Detection is advisory (mirrors :func:`redact.scan_prompt_injection`); callers
decide whether to block, warn, or ignore based on the severity mix. Unlike
the secret-redaction pipeline, this scan does NOT modify the text -- it
returns a typed :class:`Finding` list and leaves content rewriting to a
downstream step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import Finding, Severity

# --- Formula injection (CSV injection / CWE-1236) ----------------------------
# The classic spreadsheet formula trigger set: the four characters that
# spreadsheets re-interpret on cell entry. Each match has to be either at
# the start of a line (after optional whitespace) OR right after a Markdown
# table pipe (``|``) followed by optional whitespace -- Markdown tables are
# the #1 surface firecrawl / anydoc produces from Excel/CSV exports.
#
# We deliberately require a word character (or ``(`` / ``$``) immediately
# after the trigger so that benign arithmetic like ``- 5 + 3`` or a Markdown
# list ``- foo`` does NOT trigger. ``$`` covers ``=$cmd|...`` style; ``(``
# covers ``=cmd(...)`` style.
_FORMULA_TRIGGERS = ("=", "@", "+", "-")
_FORMULA_TAIL = r"[A-Za-z0-9($\\]"  # what must follow a trigger to count as injection

# Build one regex that matches any of the four triggers at line-start OR
# after a table pipe. re.MULTILINE makes ``^`` match per line; we keep the
# ``|`` alternative for in-cell detection.
_FORMULA_REGEX = re.compile(
    r"(?:^|\|)\s*(?:"
    + "|".join(re.escape(t) for t in _FORMULA_TRIGGERS)
    + r")"
    + _FORMULA_TAIL,
    re.MULTILINE,
)


# --- HTML / script injection --------------------------------------------------
# Markdown converters vary in their ``passthrough`` strictness -- common
# config lets raw HTML through. We group into a single category per
# document (HTML script) but match several distinct sub-patterns so the
# count reflects actual occurrences across the file.
_HTML_SCRIPT_REGEX = re.compile(
    r"(?:"
    # Script / iframe / object / embed -- executable semantics even if
    # converter stripped their contents. Listed first for category
    # dominance.
    r"<\s*(?:script|iframe|object|embed|form|svg|math)\b"
    r"|"
    # javascript: / vbscript: / data: URI schemes. Two surfaces:
    #   1. In an HTML attribute:  href="javascript:..." / src='vbscript:...'
    #   2. In a Markdown link:    [text](javascript:...) -- the most
    #      common firecrawl output form, no ``href=`` prefix.
    r"(?:(?:href|src|action|formaction|xlink:href)\s*=\s*[\"']?\s*|\(\s*)"
    r"(?:javascript|vbscript|data)\s*:"
    r"|"
    # Inline event handlers -- every browser still honors these.
    r"\son(?:error|load|click|mouseover|focus|blur|submit|change|input|"
    r"keydown|keyup|keypress|abort|resize|scroll|unload|toggle|wheel)\s*="
    r"|"
    # Dangerous HTML tags that stage payloads (CSS exfil, meta refresh,
    # base href hijack).
    r"<\s*/?\s*(?:base|link|meta|style|applet|frame|frameset)\b"
    r")",
    re.IGNORECASE,
)


# --- Suspicious metadata ------------------------------------------------------
# Office-suite converters frequently leave HTML-comment metadata in the
# Markdown body. We flag any HTML comment that mentions any of the leak
# keywords below; a clean Markdown document has no HTML comments at all,
# so the false-positive cost is low.
_METADATA_KEYWORDS = (
    "author",
    "revision",
    "track",
    "version",
    "modified",
    "created",
    "lastedit",
    "filename",
    "filepath",
    "owner",
    "company",
    "template",
    "lastmodifiedby",
)
_METADATA_COMMENT_REGEX = re.compile(
    r"<!--(?P<body>.*?)-->",
    re.DOTALL,
)
_METADATA_KEYWORD_REGEX = re.compile(
    r"\b(?:" + "|".join(_METADATA_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


# --- Result envelope ----------------------------------------------------------
# Distinct from :class:`redact.Result`: this scan does not produce a
# redacted string, so re-using ``Result`` would force callers to deal
# with a fake ``redacted`` field. The lighter envelope carries just what
# consumers need: ordered findings + a flag that any finding at-or-above
# the threshold should block publication.
@dataclass
class DocumentScanResult:
    """Outcome of a single :func:`scan_document` invocation.

    Attributes:
        input_chars: Length of the input Markdown text.
        findings: Findings ordered by severity descending then category.
        blocked: True when at least one finding is at or above
            ``block_severity``. Default threshold is HIGH; callers can
            override by checking ``findings`` directly.
    """

    input_chars: int
    findings: list[Finding]
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "input_chars": self.input_chars,
            "blocked": self.blocked,
            "findings": [f.to_dict() for f in self.findings],
        }


# --- Helpers ------------------------------------------------------------------
def _preview(category: str, sample: str) -> str:
    """Build a safe preview string for a finding.

    Truncates long samples and strips newlines so report-mode never
    echoes raw injection content into a single-line log line.
    """
    sample = sample.strip().replace("\n", " ")
    if len(sample) > 60:
        sample = sample[:57] + "..."
    return f"{category}: {sample!r}" if sample else category


# --- Public API ---------------------------------------------------------------
def scan_document(
    text: str,
    *,
    block_severity: Severity = Severity.HIGH,
) -> DocumentScanResult:
    """Scan a Markdown document for formula / HTML / metadata injection.

    Detection-only -- does not modify ``text``. Returns a
    :class:`DocumentScanResult` with findings ordered by severity
    descending.

    Args:
        text: Markdown content to scan.
        block_severity: Minimum severity that flips ``result.blocked``
            to True. Default ``HIGH`` matches the common "anything
            serious blocks" preflight posture; callers can pass
            ``CRITICAL`` to block only on formula injection (the most
            reliable class) or ``LOW`` to treat any suspicious surface
            as a blocker.

    Returns:
        :class:`DocumentScanResult` with findings and a ``blocked`` flag.
    """
    findings: dict[str, Finding] = {}

    # 1. Formula injection -- CRITICAL because re-importing the cell into
    # a spreadsheet (the canonical agent follow-up path) executes the
    # payload.
    formula_matches = list(_FORMULA_REGEX.finditer(text))
    if formula_matches:
        findings["formula_injection"] = Finding(
            category="formula_injection",
            severity=Severity.CRITICAL,
            count=len(formula_matches),
            preview=_preview("formula_injection", formula_matches[0].group(0)),
        )

    # 2. HTML / script injection -- HIGH because browser-rendered
    # Markdown (GitHub, GitLab, VS Code, MkDocs) honors raw HTML.
    html_matches = list(_HTML_SCRIPT_REGEX.finditer(text))
    if html_matches:
        findings["html_script"] = Finding(
            category="html_script",
            severity=Severity.HIGH,
            count=len(html_matches),
            preview=_preview("html_script", html_matches[0].group(0)),
        )

    # 3. Suspicious metadata -- MEDIUM. Information leak rather than
    # code execution, but author / revision / file-path comments are
    # the most common carry-over from Word/Excel converters and worth
    # flagging.
    metadata_comments: list[str] = []
    for m in _METADATA_COMMENT_REGEX.finditer(text):
        body = m.group("body")
        if _METADATA_KEYWORD_REGEX.search(body):
            metadata_comments.append(body)
    if metadata_comments:
        findings["metadata_leak"] = Finding(
            category="metadata_leak",
            severity=Severity.MEDIUM,
            count=len(metadata_comments),
            preview=_preview("metadata_leak", metadata_comments[0]),
        )

    ordered = sorted(findings.values(), key=lambda f: (-int(f.severity), f.category))
    blocked = any(int(f.severity) >= int(block_severity) for f in ordered)

    return DocumentScanResult(
        input_chars=len(text),
        findings=ordered,
        blocked=blocked,
    )


__all__ = [
    "DocumentScanResult",
    "scan_document",
]
