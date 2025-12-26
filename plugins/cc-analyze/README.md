# gptme-cc-analyze

Claude Code analysis subagent plugin for [gptme](https://github.com/ErikBjare/gptme).

Spawn focused Claude Code analysis tasks from within gptme. Useful for security audits, code reviews, test coverage analysis, and other focused analysis work.

## Why cc_analyze?

**Cost-efficient**: Claude Code uses a $200/mo subscription instead of per-token API costs, making it ideal for batch/parallel analysis tasks.

**Focused**: Single-purpose analysis tasks complete faster without gptme's full tool overhead.

**Parallel**: Can spawn multiple background analyses simultaneously.

**When to use cc_analyze vs gptme tools:**
- ✅ **cc_analyze**: Security scans, code reviews, coverage analysis, documentation generation
- ✅ **gptme tools**: Complex workflows, file modifications, multi-step tasks

## Installation

### 1. Prerequisites

- Claude Code CLI installed: `npm install -g @anthropic-ai/claude-code`
- Valid Claude Code subscription/authentication

### 2. Configure gptme

Add to your `gptme.toml`:

```toml
[plugins]
paths = [
    "/path/to/gptme-contrib/plugins"
]

enabled = ["cc-analyze"]
```

## Usage

### Quick Security Scan

```cc_analyze
analyze("Review this codebase for security vulnerabilities. Focus on: injection attacks, authentication issues, data exposure. List findings by severity (Critical/High/Medium/Low) with file locations.")
```

### Code Review

```cc_analyze
analyze("Review changes in the last commit for: code quality, potential bugs, performance issues, missing tests. Provide specific line-level feedback.")
```

### Test Coverage Analysis

```cc_analyze
analyze("Analyze test coverage. Identify: critical untested paths, missing edge cases, integration test gaps. Prioritize by risk.")
```

### Background Analysis (Long Tasks)

```cc_analyze
# Start long-running analysis
analyze("Comprehensive security audit of entire codebase", background=True, timeout=1800)
```

Output:
Background analysis started in session: cc_analyze_a1b2c3d4

Monitor with: tmux capture-pane -p -t cc_analyze_a1b2c3d4

### Check Background Task Progress

```cc_analyze
check_session("cc_analyze_a1b2c3d4")
```

### Kill Background Task

```cc_analyze
kill_session("cc_analyze_a1b2c3d4")
```

## Functions

### `analyze(prompt, workspace=None, timeout=600, background=False, output_format="text")`

Run a Claude Code analysis task.

**Parameters:**
- `prompt` (str): Analysis task description
- `workspace` (str, optional): Directory to run in (defaults to current)
- `timeout` (int): Max time in seconds (default: 600 = 10 minutes)
- `background` (bool): If True, run in tmux session
- `output_format` (str): "text" or "json"

**Returns:** `AnalysisResult` with output and metadata, or session ID if background=True

### `check_session(session_id)`

Check status/output of a background analysis session.

### `kill_session(session_id)`

Terminate a background analysis session.

## Example Prompts

### Security Analysis
```text
Review this codebase for security vulnerabilities. Focus on:
- Injection attacks (SQL, command, XSS)
- Authentication and authorization issues
- Data exposure and leakage
- Cryptographic weaknesses
- Dependency vulnerabilities

List findings by severity (Critical/High/Medium/Low) with:
- File location and line number
- Description of the vulnerability
- Remediation recommendation
```

### Test Coverage Analysis
```text
Analyze test coverage for this project. Identify:
- Critical paths without tests
- Missing edge case tests
- Integration test gaps
- Mock overuse hiding real issues

Prioritize findings by:
- Business impact
- Bug likelihood
- Effort to fix
```

### Architecture Review
```text
Document the architecture of this project:
- Component diagram (describe in text)
- Data flow between components
- Key abstractions and their responsibilities
- External dependencies and integrations
- Potential scalability concerns
```

## License

MIT
