"""Guard: the agent transport is email-stack-free.

Bob's hard constraint for folding agent-msg into gptmail: inter-agent messaging
must work in isolated LXC sessions with **no email infra**. That means
``transport.agent`` + ``communication_utils`` must form a closed set whose import
graph never reaches ``imaplib``, ``smtplib``, ``gptmail.lib`` (the IMAP/SMTP
``AgentEmail``), or ``gptmail.transport.email``.

This is checked at runtime in a fresh subprocess (not the test process, whose
``sys.modules`` is already polluted by other tests importing the email stack):
import only the agent transport + tracker, then assert none of the forbidden
modules loaded. See task ``fold-agent-msg-into-gptmail-single-comms-tool``.
"""

import subprocess
import sys

FORBIDDEN = ["imaplib", "smtplib", "gptmail.lib", "gptmail.transport.email"]

_PROBE = """
import sys
# The two modules that MUST stay isolated, imported exactly as an LXC session would.
from gptmail.transport.agent import AgentTransport
from gptmail.communication_utils.state.tracking import ConversationTracker, MessageInfo

forbidden = {forbidden!r}
leaked = [m for m in forbidden if m in sys.modules]
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)
print("CLEAN")
""".format(forbidden=FORBIDDEN)


def test_agent_transport_imports_no_email_stack() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"agent transport pulled in the email stack.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "CLEAN" in result.stdout
