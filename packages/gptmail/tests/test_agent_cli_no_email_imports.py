"""Guard: the ``gptmail agent`` CLI subgroup is email-stack-free.

Same constraint as the transport guard, one layer up: importing ``agent_cli``
(the ``gptmail agent`` commands) must not pull in ``imaplib``/``smtplib``/
``gptmail.lib``/``gptmail.transport.email``, so the inter-agent CLI runs in
isolated LXC sessions with no email infra. The unified ``gptmail`` entry point
(``cli.py``) deliberately *does* import the email stack — that's why the agent
commands live in their own module and are tested for isolation here, not via
``cli``. See task ``fold-agent-msg-into-gptmail-single-comms-tool``.
"""

import subprocess
import sys

FORBIDDEN = ["imaplib", "smtplib", "gptmail.lib", "gptmail.transport.email"]

_PROBE = """
import sys
from gptmail.agent_cli import agent  # noqa: F401

forbidden = {forbidden!r}
leaked = [m for m in forbidden if m in sys.modules]
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)
print("CLEAN")
""".format(forbidden=FORBIDDEN)


def test_agent_cli_imports_no_email_stack() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"agent_cli pulled in the email stack.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "CLEAN" in result.stdout
