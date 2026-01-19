#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "rich>=13.0.0",
#   "google-api-python-client>=2.100.0",
#   "google-auth-oauthlib>=1.0.0",
#   "google-auth-httplib2>=0.1.0",
# ]
# [tool.uv]
# exclude-newer = "2026-01-01T00:00:00Z"
# ///
"""
Google Drive tool for gptme agents.

Enables searching, reading, and listing Google Docs and Drive files.
Requires OAuth2 credentials setup (see --setup for details).

Usage:
    ./google_drive.py search "meeting notes"
    ./google_drive.py read DOCUMENT_ID
    ./google_drive.py recent [--max-results 10]
    ./google_drive.py list-folder FOLDER_ID
    ./google_drive.py --setup  # First-time setup instructions
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Scopes for Google API access
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]

console = Console()

# Default paths for credentials
DEFAULT_CREDENTIALS_PATH = Path("~/.config/gptme/google_credentials.json").expanduser()
DEFAULT_TOKEN_PATH = Path("~/.config/gptme/google_token.json").expanduser()


def get_credentials_paths() -> tuple[Path, Path]:
    """Get paths for credentials and token files from environment or defaults."""
    creds_path = Path(
        os.environ.get("GOOGLE_CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH)
    ).expanduser()
    token_path = Path(
        os.environ.get("GOOGLE_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    ).expanduser()
    return creds_path, token_path


def get_google_service(service_name: str = "drive", version: str = "v3"):
    """
    Get authenticated Google API service.

    Args:
        service_name: API service name ('drive' or 'docs')
        version: API version ('v3' for drive, 'v1' for docs)

    Returns:
        Authenticated Google API service object
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds_path, token_path = get_credentials_paths()

    creds = None

    # Load existing token
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # Refresh or create new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                console.print(
                    f"[red]Error:[/red] Credentials file not found at {creds_path}"
                )
                console.print("\nRun with --setup for setup instructions.")
                sys.exit(1)

            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the token with restricted permissions (owner read/write only)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as token_file:
            token_file.write(creds.to_json())
        # Set restrictive permissions for security (0o600 = owner read/write only)
        os.chmod(token_path, 0o600)
        console.print(f"[green]Token saved to {token_path}[/green]")

    return build(service_name, version, credentials=creds)


def extract_doc_text(content: list[dict[str, Any]]) -> str:
    """
    Extract plain text from Google Docs content structure.

    Args:
        content: The 'body.content' from a Google Docs API response

    Returns:
        Extracted plain text
    """
    text_parts = []

    def extract_elements(elements: list[dict[str, Any]]) -> None:
        if not elements:
            return
        for element in elements:
            if "paragraph" in element:
                for elem in element["paragraph"].get("elements", []):
                    if "textRun" in elem:
                        text_parts.append(elem["textRun"].get("content", ""))
            if "table" in element:
                for row in element["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        extract_elements(cell.get("content", []))

    extract_elements(content)
    return "".join(text_parts)


@click.group(invoke_without_command=True)
@click.option("--setup", is_flag=True, help="Show setup instructions")
@click.pass_context
def cli(ctx: click.Context, setup: bool) -> None:
    """Google Drive tool for gptme agents."""
    if setup:
        show_setup_instructions()
        ctx.exit()
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def show_setup_instructions() -> None:
    """Display setup instructions for Google OAuth credentials."""
    instructions = """
[bold]Google Drive Tool Setup[/bold]

This tool requires OAuth2 credentials to access Google Drive and Docs.

[bold]Step 1: Create OAuth Credentials[/bold]

1. Go to Google Cloud Console: https://console.cloud.google.com/
2. Create a new project or select an existing one
3. Enable the Google Drive API and Google Docs API:
   - APIs & Services → Library
   - Search "Google Drive API" → Enable
   - Search "Google Docs API" → Enable
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials
   - Create Credentials → OAuth client ID
   - Application type: Desktop app
   - Download the credentials JSON file

[bold]Step 2: Configure the Tool[/bold]

Save the downloaded JSON file to one of:
- ~/.config/gptme/google_credentials.json (default)
- Or set GOOGLE_CREDENTIALS_PATH environment variable

[bold]Step 3: Authorize[/bold]

Run any command (e.g., `./google_drive.py recent`) and follow the
browser-based OAuth flow to authorize access.

The token will be saved to:
- ~/.config/gptme/google_token.json (default)
- Or set GOOGLE_TOKEN_PATH environment variable

[bold]Environment Variables[/bold]

- GOOGLE_CREDENTIALS_PATH: Path to OAuth credentials JSON
- GOOGLE_TOKEN_PATH: Path to save/load OAuth token
"""
    console.print(Panel(instructions, title="Setup Instructions", border_style="blue"))


@cli.command()
@click.argument("query")
@click.option("--max-results", "-n", default=20, help="Maximum results to return")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def search(query: str, max_results: int, as_json: bool) -> None:
    """Search for documents in Google Drive."""
    try:
        service = get_google_service("drive", "v3")

        # Build query for Google Docs
        # Escape single quotes in query to prevent query injection
        escaped_query = query.replace("'", "\\'")
        search_query = (
            f"name contains '{escaped_query}' and "
            f"mimeType = 'application/vnd.google-apps.document'"
        )

        results = (
            service.files()
            .list(
                q=search_query,
                fields="files(id, name, createdTime, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc",
                pageSize=max_results,
            )
            .execute()
        )

        files = results.get("files", [])

        if as_json:
            print(json.dumps(files, indent=2))
            return

        if not files:
            console.print(f"[yellow]No documents found matching '{query}'[/yellow]")
            return

        table = Table(title=f"Search Results: '{query}'")
        table.add_column("ID", style="dim")
        table.add_column("Name", style="cyan")
        table.add_column("Modified", style="green")
        table.add_column("URL", style="blue")

        for file in files:
            modified = file.get("modifiedTime", "")[:10]
            table.add_row(
                file["id"][:15] + "...",
                file["name"][:50],
                modified,
                file.get("webViewLink", "")[:40] + "...",
            )

        console.print(table)
        console.print(f"\n[dim]Found {len(files)} document(s)[/dim]")

    except Exception as e:
        console.print(f"[red]Error searching:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.argument("document_id")
@click.option(
    "--format",
    "-f",
    "output_format",
    default="text",
    type=click.Choice(["text", "json", "markdown"]),
    help="Output format",
)
def read(document_id: str, output_format: str) -> None:
    """Read a Google Doc by its ID."""
    try:
        service = get_google_service("docs", "v1")

        document = service.documents().get(documentId=document_id).execute()

        if output_format == "json":
            print(json.dumps(document, indent=2))
            return

        title = document.get("title", "Untitled")
        content = document.get("body", {}).get("content", [])
        text = extract_doc_text(content)

        if output_format == "markdown":
            console.print(f"# {title}\n")
            console.print(text)
        else:
            console.print(Panel(f"[bold]{title}[/bold]", border_style="blue"))
            console.print(text)

    except Exception as e:
        console.print(f"[red]Error reading document:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.option("--max-results", "-n", default=10, help="Maximum results to return")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def recent(max_results: int, as_json: bool) -> None:
    """List recently modified Google Docs."""
    try:
        service = get_google_service("drive", "v3")

        results = (
            service.files()
            .list(
                q="mimeType = 'application/vnd.google-apps.document'",
                fields="files(id, name, createdTime, modifiedTime, webViewLink, owners)",
                orderBy="modifiedTime desc",
                pageSize=max_results,
            )
            .execute()
        )

        files = results.get("files", [])

        if as_json:
            print(json.dumps(files, indent=2))
            return

        if not files:
            console.print("[yellow]No recent documents found[/yellow]")
            return

        table = Table(title="Recent Google Docs")
        table.add_column("ID", style="dim", width=18)
        table.add_column("Name", style="cyan", max_width=40)
        table.add_column("Modified", style="green", width=12)
        table.add_column("Owner", style="yellow", width=20)

        for file in files:
            modified = file.get("modifiedTime", "")[:10]
            owners = file.get("owners", [])
            owner_name = (
                owners[0].get("displayName", "Unknown") if owners else "Unknown"
            )

            # Truncate name if needed
            name = file["name"]
            if len(name) > 40:
                name = name[:37] + "..."

            table.add_row(
                file["id"][:18],
                name,
                modified,
                owner_name[:20],
            )

        console.print(table)
        console.print(f"\n[dim]Showing {len(files)} most recent document(s)[/dim]")
        console.print("\n[dim]Use 'read <ID>' to read a document's content[/dim]")

    except Exception as e:
        console.print(f"[red]Error listing documents:[/red] {e}")
        sys.exit(1)


@cli.command("list-folder")
@click.argument("folder_id")
@click.option("--max-results", "-n", default=50, help="Maximum results to return")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_folder(folder_id: str, max_results: int, as_json: bool) -> None:
    """List contents of a Google Drive folder."""
    try:
        service = get_google_service("drive", "v3")

        results = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents",
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc",
                pageSize=max_results,
            )
            .execute()
        )

        files = results.get("files", [])

        if as_json:
            print(json.dumps(files, indent=2))
            return

        if not files:
            console.print("[yellow]Folder is empty or inaccessible[/yellow]")
            return

        table = Table(title="Folder Contents")
        table.add_column("Type", style="dim", width=8)
        table.add_column("ID", style="dim", width=18)
        table.add_column("Name", style="cyan", max_width=40)
        table.add_column("Modified", style="green", width=12)

        for file in files:
            mime_type = file.get("mimeType", "")
            if "folder" in mime_type:
                file_type = "📁 Folder"
            elif "document" in mime_type:
                file_type = "📄 Doc"
            elif "spreadsheet" in mime_type:
                file_type = "📊 Sheet"
            else:
                file_type = "📎 File"

            modified = file.get("modifiedTime", "")[:10]

            table.add_row(
                file_type,
                file["id"][:18],
                file["name"][:40],
                modified,
            )

        console.print(table)
        console.print(f"\n[dim]Found {len(files)} item(s)[/dim]")

    except Exception as e:
        console.print(f"[red]Error listing folder:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.argument("document_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def info(document_id: str, as_json: bool) -> None:
    """Get metadata about a document."""
    try:
        service = get_google_service("drive", "v3")

        file_info = (
            service.files()
            .get(
                fileId=document_id,
                fields="id,name,mimeType,createdTime,modifiedTime,owners,webViewLink,description",
            )
            .execute()
        )

        if as_json:
            print(json.dumps(file_info, indent=2))
            return

        console.print(
            Panel(
                f"[bold]{file_info.get('name', 'Unknown')}[/bold]", border_style="blue"
            )
        )

        info_lines = [
            f"[bold]ID:[/bold] {file_info.get('id', 'N/A')}",
            f"[bold]Type:[/bold] {file_info.get('mimeType', 'N/A')}",
            f"[bold]Created:[/bold] {file_info.get('createdTime', 'N/A')[:10]}",
            f"[bold]Modified:[/bold] {file_info.get('modifiedTime', 'N/A')[:10]}",
            f"[bold]URL:[/bold] {file_info.get('webViewLink', 'N/A')}",
        ]

        owners = file_info.get("owners", [])
        if owners:
            owner_names = [o.get("displayName", "Unknown") for o in owners]
            info_lines.append(f"[bold]Owner(s):[/bold] {', '.join(owner_names)}")

        if file_info.get("description"):
            info_lines.append(f"[bold]Description:[/bold] {file_info['description']}")

        console.print("\n".join(info_lines))

    except Exception as e:
        console.print(f"[red]Error getting info:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
