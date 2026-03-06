"""
Setup helpers for gptme-whatsapp.

Checks Node.js availability and installs npm dependencies.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import click

NODE_DIR = Path(__file__).parent.parent.parent.parent / "node"


def check_node():
    """Check that Node.js >= 18 is available."""
    node = shutil.which("node")
    if not node:
        print(
            "Error: Node.js not found. Install with: nvm install --lts", file=sys.stderr
        )
        return False

    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    version = result.stdout.strip().lstrip("v")
    major = int(version.split(".")[0])
    if major < 18:
        print(f"Error: Node.js {version} is too old. Need >= 18.", file=sys.stderr)
        return False

    return True


def install_npm_deps():
    """Install npm dependencies for the whatsapp bridge."""
    if not check_node():
        return False

    npm = shutil.which("npm")
    if not npm:
        print("Error: npm not found.", file=sys.stderr)
        return False

    print(f"Installing npm dependencies in {NODE_DIR}...")
    result = subprocess.run(["npm", "install"], cwd=NODE_DIR)
    return result.returncode == 0


def generate_systemd_service(
    agent_name: str,
    workspace: str,
    allowed_contacts: list[str],
    node_path: str = "/usr/local/bin/node",
    backend: str = "gptme",
    claude_path: str = "",
) -> str:
    """Generate a systemd service file for the WhatsApp bridge."""
    allowed = ",".join(allowed_contacts)
    env_lines = [
        f"Environment=GPTME_AGENT={agent_name}",
        f"Environment=AGENT_WORKSPACE={workspace}",
        f"Environment=ALLOWED_CONTACTS={allowed}",
        f"Environment=BACKEND={backend}",
    ]
    path_parts = [node_path]
    if claude_path:
        path_parts.append(claude_path)
    path_parts.extend(
        ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    )
    env_lines.append(f"Environment=PATH={':'.join(path_parts)}")

    service = f"""[Unit]
Description=gptme WhatsApp bridge for {agent_name}
After=network.target

[Service]
Type=simple
WorkingDirectory={NODE_DIR}
{chr(10).join(env_lines)}
ExecStart=/usr/bin/node {NODE_DIR}/index.js
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    return service


@click.group()
def main():
    """gptme-whatsapp setup."""


@main.command()
def install():
    """Install npm dependencies."""
    if install_npm_deps():
        print("npm dependencies installed successfully.")
    else:
        sys.exit(1)


@main.command()
@click.option("--agent", required=True, help="Agent name (e.g. sven)")
@click.option("--workspace", required=True, help="Agent workspace path")
@click.option("--contacts", multiple=True, help="Allowed phone numbers")
@click.option("--node-path", default="/usr/local/bin", help="Node.js bin path")
@click.option(
    "--backend",
    default="gptme",
    type=click.Choice(["gptme", "claude-code"]),
    help="Agent backend",
)
@click.option("--claude-path", default="", help="Claude Code bin path")
def service(
    agent: str,
    workspace: str,
    contacts: tuple,
    node_path: str,
    backend: str,
    claude_path: str,
):
    """Generate systemd service."""
    service_text = generate_systemd_service(
        agent,
        workspace,
        list(contacts),
        node_path,
        backend,
        claude_path,
    )
    print(service_text)


if __name__ == "__main__":
    main()
