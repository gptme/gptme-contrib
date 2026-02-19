"""
Setup helpers for gptme-whatsapp.

Checks Node.js availability and installs npm dependencies.
"""

import shutil
import subprocess
import sys
from pathlib import Path

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
) -> str:
    """Generate a systemd service file for the WhatsApp bridge."""
    allowed = ",".join(allowed_contacts)
    service = f"""[Unit]
Description=gptme WhatsApp bridge for {agent_name}
After=network.target

[Service]
Type=simple
WorkingDirectory={NODE_DIR}
Environment=GPTME_AGENT={agent_name}
Environment=AGENT_WORKSPACE={workspace}
Environment=ALLOWED_CONTACTS={allowed}
Environment=PATH={node_path}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/node {NODE_DIR}/index.js
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    return service


def main():
    """CLI entry point for setup."""
    import argparse

    parser = argparse.ArgumentParser(description="gptme-whatsapp setup")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("install", help="Install npm dependencies")

    service_parser = subparsers.add_parser("service", help="Generate systemd service")
    service_parser.add_argument("--agent", required=True, help="Agent name (e.g. sven)")
    service_parser.add_argument(
        "--workspace", required=True, help="Agent workspace path"
    )
    service_parser.add_argument("--contacts", nargs="+", help="Allowed phone numbers")
    service_parser.add_argument(
        "--node-path", default="/usr/local/bin", help="Node.js bin path"
    )

    args = parser.parse_args()

    if args.command == "install":
        if install_npm_deps():
            print("npm dependencies installed successfully.")
        else:
            sys.exit(1)
    elif args.command == "service":
        contacts = args.contacts or []
        service = generate_systemd_service(
            args.agent, args.workspace, contacts, args.node_path
        )
        print(service)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
