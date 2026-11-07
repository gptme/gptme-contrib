"""Shared theme: the token file, base page styles and the manual theme toggle.

``tokens.css`` is shipped as a file, not a Python string per generator. Bob's
workspace alone carried 13 independent ``:root`` palettes before this.
"""

from __future__ import annotations

from importlib import resources

TOKENS_CSS: str = (
    resources.files("gptme_vitals").joinpath("tokens.css").read_text(encoding="utf-8")
)

# Base type, stat tile and badges. Colours come only from tokens.css variables,
# so the same rules render correctly in light and dark.
BASE_CSS = """
body {
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, 'Inter', system-ui, 'Segoe UI', sans-serif;
  background: var(--bg); color: var(--text); font-variant-numeric: tabular-nums;
}
button, input, select, textarea { font: inherit; }
svg text { font-family: inherit; }
.stat { border-left: 3px solid var(--stat-status, var(--border)); padding: 8px 12px;
  background: var(--tile-bg); border-radius: 4px; }
code, pre { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 12px; }
a { color: var(--accent); }
.badge { display: inline-block; font-size: 10px; padding: 2px 7px; border-radius: 4px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.badge-ok, .badge-unknown { background: var(--badge-ok-bg); color: var(--dim); }
.badge-green { background: var(--badge-green-bg); color: var(--green); }
.badge-yellow { background: var(--badge-yellow-bg); color: var(--yellow); }
.badge-red { background: var(--badge-red-bg); color: var(--red); }
.theme-toggle { background: transparent; color: var(--dim); border: 1px solid var(--border);
  border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 12px; }
.theme-toggle:hover { color: var(--accent); }
""".strip()

THEME_STORAGE_KEY = "gptme-vitals-theme"

# Runs in <head> before first paint so a pinned theme does not flash. Storage can
# throw (privacy modes, sandboxed iframes); the page then just follows the OS.
THEME_INIT_JS = (
    "try{var t=localStorage.getItem('" + THEME_STORAGE_KEY + "');"
    "if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}catch(e){}"
)

# Cycles: follow OS -> light -> dark -> follow OS. Absent data-theme *is* the
# "follow the OS" state, so removing the attribute and the stored key restores it.
_TOGGLE_JS = (
    "(function(){var b=document.currentScript.previousElementSibling,"
    "r=document.documentElement,k='" + THEME_STORAGE_KEY + "',"
    "o=['','light','dark'];"
    "function show(){b.textContent='Theme: '+(r.dataset.theme||'auto');}"
    "b.addEventListener('click',function(){"
    "var n=o[(o.indexOf(r.dataset.theme||'')+1)%o.length];"
    "if(n){r.dataset.theme=n;}else{delete r.dataset.theme;}"
    "try{if(n){localStorage.setItem(k,n);}else{localStorage.removeItem(k);}}catch(e){}"
    "show();});show();})();"
)


def render_head_assets() -> str:
    """``<style>`` (tokens + base) and the pre-paint theme script for ``<head>``."""
    return (
        f"<style>\n{TOKENS_CSS}\n{BASE_CSS}\n</style>\n"
        f"<script>{THEME_INIT_JS}</script>"
    )


def render_theme_toggle() -> str:
    """A button that cycles auto/light/dark; the script binds to the button before it."""
    return (
        '<button type="button" class="theme-toggle" aria-label="Switch colour theme">'
        "Theme: auto</button>"
        f"<script>{_TOGGLE_JS}</script>"
    )
