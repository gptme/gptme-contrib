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
from html import unescape
from html.parser import HTMLParser

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
    # URI destinations and event attributes are scanned separately using the
    # HTML parser, not by matching attribute-shaped text in prose.
    # Dangerous HTML tags that stage payloads (CSS exfil, meta refresh,
    # base href hijack).
    r"<\s*/?\s*(?:base|link|meta|style|applet|frame|frameset)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


# Inline and reference-definition destinations. The bounded definition indent
# and link opener avoid interpreting entity-escaped HTML/prose as a destination.
_MARKDOWN_URI_REGEX = re.compile(
    r"(?:\]\(\s*|^[ ]{0,3}\[(?:\\.|[^\]\\\r\n])+\]:"
    r"[ \t]*(?:\r?\n[ \t]*)?)"
    r"(?:<(?P<angle>[^<>\r\n]*)>|(?P<bare>[^\s)<>]+))",
    re.MULTILINE,
)

_URI_ATTRIBUTES = frozenset({"href", "src", "action", "formaction", "xlink:href"})
_DANGEROUS_SCHEME_REGEX = re.compile(
    r"^(?:javascript|vbscript|data)\s*:", re.IGNORECASE
)
# CommonMark backslash escapes apply only to ASCII punctuation. Process these
# and character references together so an escaped ampersand is not decoded twice.
_MARKDOWN_ESCAPE_REGEX = re.compile(
    r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])"
    r"|&(?:\#[xX][0-9a-fA-F]{1,6}|\#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]+);"
)
_URL_EDGE_CONTROLS = "".join(chr(c) for c in range(33))
_URL_IGNORED_WHITESPACE = str.maketrans("", "", "\t\n\r")


class _HtmlUriScanner(HTMLParser):
    """Check actual HTML URI and event attributes, not entity-escaped prose.

    HTMLParser decodes character references in attribute values exactly once.
    Never unescape the document: escaped tags and payload examples are text,
    and decoding them globally would create spurious findings.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.matches: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            # Event handlers form an extensible browser attribute family. Treat
            # on* attributes conservatively rather than maintaining a stale list.
            if re.fullmatch(r"on[a-z]+", name) and value is not None:
                self.matches.append(self.get_starttag_text() or tag)
                continue
            if name not in _URI_ATTRIBUTES or value is None:
                continue
            # URL parsing removes tabs/newlines anywhere and leading C0
            # controls/spaces before interpreting the scheme.
            normalized = value.translate(_URL_IGNORED_WHITESPACE).lstrip(
                _URL_EDGE_CONTROLS
            )
            if _DANGEROUS_SCHEME_REGEX.match(normalized):
                self.matches.append(self.get_starttag_text() or tag)


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
    uri_scanner = _HtmlUriScanner()
    uri_scanner.feed(text)
    uri_scanner.close()
    html_matches = [m.group(0) for m in _HTML_SCRIPT_REGEX.finditer(text)]
    html_matches.extend(uri_scanner.matches)
    for match in _MARKDOWN_URI_REGEX.finditer(text):
        destination = match.group("angle") or match.group("bare") or ""
        decoded = _MARKDOWN_ESCAPE_REGEX.sub(
            lambda m: m.group(1) if m.group(1) is not None else unescape(m.group(0)),
            destination,
        )
        normalized = decoded.translate(_URL_IGNORED_WHITESPACE).lstrip(
            _URL_EDGE_CONTROLS
        )
        if _DANGEROUS_SCHEME_REGEX.match(normalized):
            html_matches.append(match.group(0))
    if html_matches:
        findings["html_script"] = Finding(
            category="html_script",
            severity=Severity.HIGH,
            count=len(html_matches),
            preview=_preview("html_script", html_matches[0]),
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
