"""Shared URL validation utilities for twitter scripts."""

import re
import urllib.error
import urllib.request


def validate_urls_in_text(text: str) -> list[tuple[str, int]]:
    """HEAD-check any http(s) URLs in tweet text. Return [(url, status)] for unreachable ones (4xx/5xx).

    Network errors are treated as non-fatal (returns nothing for those URLs) — the goal is to
    catch deterministic 404s like the recurring `/YYYY/MM/DD/title/` vs `/blog/title/` permalink
    bug, not to require connectivity. Mirrors the check in scripts/twitter/post-blog-tweet.py.
    """
    url_pattern = re.compile(r"https?://[^\s\"'<>)]+")
    bad: list[tuple[str, int]] = []
    for url in url_pattern.findall(text):
        url = url.rstrip(".,;:!?")
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0 (URL checker)")
            with urllib.request.urlopen(req, timeout=5):
                pass  # 4xx/5xx raise HTTPError before reaching here
        except urllib.error.HTTPError as e:
            bad.append((url, e.code))
        except Exception:
            pass
    return bad
