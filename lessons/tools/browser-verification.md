---
match:
  keywords:
  - browser
  - verification
  - console
  - playwright
  - web
  - deployment
---

# Browser Verification for Web Changes

## Rule
Always verify web changes by checking console outputs and visual rendering before declaring them fixed.

## Context
When making changes to web applications (HTML, CSS, JavaScript) that will be deployed to production.

## Detection
Observable signals that you need browser verification:
- About to commit web changes without loading the page
- Claiming "Fixed!" based on code review alone
- JavaScript errors might be present but unchecked
- Deployment completed but not verified
- Assuming fix worked without seeing it run

Common failure patterns (from logs):
- Fixed JS reference → didn't load page → errors still present
- Updated HTML → didn't check console → missing elements broke functionality
- Deployed changes → assumed worked → users found issues

## Pattern

**Minimal 3-step verification**:
```python
from gptme.tools.browser import read_url, read_logs, screenshot_url

# Step 1: Load and check content
url = "https://example.com/page"
content = read_url(url)
assert "Expected content" in content

# Step 2: Check console for errors
logs = read_logs()
assert "Error" not in logs  # No JavaScript errors

# Step 3: Visual verification
screenshot_url(url, "verify.png")
# Review screenshot for correct appearance
```

## Outcome
Following this pattern results in:
- **Catch issues early**: Console logs show errors immediately
- **Single deploy cycle**: Fix once instead of multiple attempts
- **Professional workflow**: Demonstrates thorough testing
- **User confidence**: No production surprises

Example (from real incident):
- Without verification: "Fixed!" → still broken → second fix needed
- With verification: Caught missing elements → fixed first time
