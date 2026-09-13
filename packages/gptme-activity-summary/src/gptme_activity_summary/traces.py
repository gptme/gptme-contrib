"""Private, durable invocation directories shared by summary backends."""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def create_trace_dir(prefix: str) -> Path:
    """Allocate an isolated 0700 directory; never automatically delete traces."""
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    root = root / "gptme-activity-summary"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root)).resolve()


def save_output(root: Path, stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    """Retain full CLI diagnostics, including partial output after timeout."""
    for name, output in (("stdout.log", stdout), ("stderr.log", stderr)):
        try:
            data = output.encode("utf-8") if isinstance(output, str) else output or b""
            (root / name).write_bytes(data)
        except OSError as exc:
            logger.warning("Cannot save %s in %s: %s", name, root, exc)
