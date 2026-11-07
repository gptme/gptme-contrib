# gptme-redact

Secret-safe export redactor — layered, fail-closed redaction for publishing derived agent content safely.

**Distribution name:** `gptme-redact`. **Import module:** `redact`. The
distribution is renamed because PyPI `redact` 0.1.1 is an unrelated
config-template renderer; importing `redact` still works, so a bare
`redact` requirement is a trap that resolves to the wrong project.

```bash
uv tool install gptme-redact        # provides the `redact` CLI
uv pip install "gptmail[secrets]"   # gptmail's outbound scanner extra
```

## What it does

A six-layer redaction pipeline that blocks artifacts containing secrets before they leave the agent workspace. Fail-closed: any finding at the blocking severity (default: `critical`) rejects the whole artifact rather than silently redacting it.

| Layer | What | Mechanism |
|-------|------|-----------|
| 1. Deterministic | Strip exact secrets | Env values + explicit literals |
| 2. Pattern | Regex deny-list | Built-in patterns for API keys, tokens, URLs |
| 3. TruffleHog | Optional binary scan | `trufflehog filesystem` integration |
| 4. LLM review | Judge safety | *(external, not in this package)* |
| 5. Human gate | Report / reject | `--report` mode + fail-closed exit |
| 6. Prompt-injection | Detect injection surfaces | Advisory scan in untrusted text |

## Quick Start

```bash
# Redact a file (blocking on critical findings)
redact my-file.md

# Report mode — see what would be redacted without writing
redact my-file.md --report

# Machine-readable report (for tooling)
redact my-file.md --json

# Add extra literals and run TruffleHog scan
redact my-file.md --literal "my-secret-token" --trufflehog
```

## Usage

```txt
usage: redact [-h] [--out OUT] [--report] [--json] [--literal LITERAL]
              [--allowlist ALLOWLIST] [--no-default-allowlist]
              [--trufflehog] [--scan-injection]
              [--block-severity BLOCK_SEVERITY] [--no-env-secrets]
              file

positional arguments:
  file                  file to redact (use - for stdin)

options:
  --out OUT             write redacted text here
  --report              print findings report instead of redacted text
  --json                machine-readable report (implies --report)
  --literal LITERAL     extra literal secret to redact (repeatable)
  --allowlist ALLOWLIST
                        exempt exact literal from non-critical redaction
  --no-default-allowlist
                        skip the built-in known-public allowlist
  --trufflehog          enable layer 3 TruffleHog scanning
  --scan-injection      enable layer 6 prompt-injection surface scan
  --block-severity      minimum severity that blocks (default: critical)
  --no-env-secrets      skip env-value redaction (deterministic mode)
```

## Python API

```python
from redact import redact, collect_secret_literals, Result

result = redact(
    text="my API key is sk-ant-api...",
    literals=collect_secret_literals(),  # auto-collects env vars
    allowlist=["https://example.com/public-endpoint"],
    block_severity=Severity.CRITICAL,
)

if result.blocked:
    print("Blocked:", result.block_reasons)
else:
    print(result.redacted)
```

## Prompt-Injection Scan

```python
from redact import scan_prompt_injection

findings = scan_prompt_injection(untrusted_user_text)
# [Finding(category="injection:system_override", severity=Severity.HIGH, ...)]
```

The injection scan is advisory — it flags suspicious content without modifying the text. Use it as a preflight gate before feeding scraped or user-supplied text into an LLM context.

## Development

```bash
# Run tests
uv run pytest packages/gptme-redact/tests/ -v

# Run CLI on a test file
uv run python3 -m redact.cli path/to/file.md --report
```

## Related

- [Idea backlog entry #362](https://github.com/ErikBjare/bob/issues/362) — original design
- `knowledge/processes/fail-closed-redaction.md` — operational guide
