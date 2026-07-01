"""gptme-wisdom CLI — ingest, search, list, and remove reference-book chunks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .indexer import BookIndex, DEFAULT_DB_PATH
from .parsers import parse_book_text

# Curated seed sources with freely-licensed metadata.
SOURCES: dict[str, dict[str, str]] = {
    "sicp": {
        "title": "Structure and Interpretation of Computer Programs",
        "url": "https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/index.html",
        "license": "CC BY-SA 4.0",
    },
    "ostep": {
        "title": "Operating Systems: Three Easy Pieces",
        "url": "https://pages.cs.wisc.edu/~remzi/OSTEP/",
        "license": "free (author-hosted)",
    },
    "rl-intro": {
        "title": "Reinforcement Learning: An Introduction (2nd ed.)",
        "url": "http://incompleteideas.net/book/the-book-2nd.html",
        "license": "free (author-hosted)",
    },
    "thinkpython": {
        "title": "Think Python: How to Think Like a Computer Scientist",
        "url": "https://greenteapress.com/wp/think-python-2e/",
        "license": "CC BY-NC 3.0",
    },
    "mml-book": {
        "title": "Mathematics for Machine Learning",
        "url": "https://mml-book.github.io/",
        "license": "CC BY-NC-SA 4.0",
    },
    "pro-git": {
        "title": "Pro Git",
        "url": "https://git-scm.com/book/en/v2",
        "license": "CC BY-NC-SA 3.0",
    },
    "eloquentjs": {
        "title": "Eloquent JavaScript",
        "url": "https://eloquentjavascript.net/",
        "license": "CC BY-NC 3.0",
    },
}


@click.group()
@click.option(
    "--db",
    type=click.Path(dir_okay=False),
    default=str(DEFAULT_DB_PATH),
    show_default=True,
    help="Path to wisdom SQLite DB.",
)
@click.pass_context
def main(ctx: click.Context, db: str) -> None:
    """gptme-wisdom — BM25-searchable reference-book index for gptme agents.

    Ingest plain-text or markdown book dumps, then search them with keyword
    queries. Results include chapter/section provenance and license information
    for citation.

    Quick start:

    \b
        # Download a freely-licensed book, then ingest:
        gptme-wisdom ingest --source sicp --file sicp.txt

        # Search:
        gptme-wisdom search "tail call optimization"

        # List indexed books:
        gptme-wisdom list
    """
    ctx.ensure_object(dict)
    ctx.obj["db"] = Path(db)


@main.command()
@click.option(
    "--file",
    required=True,
    type=click.Path(exists=True),
    help="Local book text/markdown file.",
)
@click.option(
    "--source", required=True, help="Source slug (e.g. sicp). Curated slugs autofill metadata."
)
@click.option("--title", help="Book title (overrides curated metadata).")
@click.option("--url", default="", help="Source URL (overrides curated metadata).")
@click.option(
    "--license", "license_", default="", help="License string (overrides curated metadata)."
)
@click.option(
    "--target-tokens", default=1000, show_default=True, help="Target chunk size in tokens."
)
@click.option(
    "--overlap-tokens", default=100, show_default=True, help="Overlap between adjacent chunks."
)
@click.option(
    "--min-chunk-tokens", default=50, show_default=True, help="Minimum chunk size (drop smaller)."
)
@click.pass_context
def ingest(
    ctx: click.Context,
    file: str,
    source: str,
    title: str | None,
    url: str,
    license_: str,
    target_tokens: int,
    overlap_tokens: int,
    min_chunk_tokens: int,
) -> None:
    """Ingest a book text file into the wisdom index.

    Curated source slugs (sicp, ostep, rl-intro, thinkpython, mml-book,
    pro-git, eloquentjs) autofill title/url/license. Pass --title to use an
    arbitrary book.

    \b
    Examples:
        gptme-wisdom ingest --source sicp --file sicp.md
        gptme-wisdom ingest --source mybook --title "My Book" --file mybook.txt
    """
    db: Path = ctx.obj["db"]
    curated = SOURCES.get(source, {})
    effective_title = title or curated.get("title")
    effective_url = url or curated.get("url", "")
    effective_license = license_ or curated.get("license", "unknown")

    if not effective_title:
        known = ", ".join(sorted(SOURCES))
        click.echo(
            f"error: no title for source '{source}' — pass --title " f"(curated slugs: {known})",
            err=True,
        )
        sys.exit(1)

    text = Path(file).read_text(encoding="utf-8", errors="replace")
    docs = parse_book_text(
        text,
        source=source,
        title=effective_title,
        url=effective_url,
        license=effective_license,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        min_chunk_tokens=min_chunk_tokens,
    )
    if not docs:
        click.echo("warning: no chunks produced (empty file or too short?)", err=True)
        sys.exit(1)

    with BookIndex(db_path=db) as idx:
        added = idx.add_many(iter(docs))
        total = idx.count(source=source)

    click.echo(
        f"ingested {source}: {len(docs)} chunks parsed, {added} new "
        f"({total} total for source) → {db}"
    )


@main.command()
@click.argument("query")
@click.option("--source", default=None, help="Restrict to a source slug.")
@click.option("--limit", default=5, show_default=True, help="Max results.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
@click.option("--snippet-chars", default=280, show_default=True, help="Snippet length (text mode).")
@click.option("--context/--no-context", default=False, help="Output as gptme context block.")
@click.pass_context
def search(
    ctx: click.Context,
    query: str,
    source: str | None,
    limit: int,
    as_json: bool,
    snippet_chars: int,
    context: bool,
) -> None:
    """Search the wisdom index with a BM25 keyword query.

    \b
    Examples:
        gptme-wisdom search "virtual memory page tables"
        gptme-wisdom search "amortized complexity" --source sicp --json
        gptme-wisdom search "reinforcement learning policy" --context
    """
    db: Path = ctx.obj["db"]
    if not db.exists():
        click.echo(
            f"error: no wisdom index at {db} — run `gptme-wisdom ingest` first",
            err=True,
        )
        sys.exit(1)

    with BookIndex(db_path=db) as idx:
        results = idx.search(query, source=source, limit=limit)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo("(no matches)")
        return

    if context:
        # Compact context-injection format for gptme context_cmd
        click.echo(f"## Wisdom: {query}\n")
        for r in results:
            loc = " › ".join(p for p in (r["chapter"], r["section"]) if p)
            header = f"**[{r['source']}] {r['title']}**"
            if loc:
                header += f" — {loc}"
            click.echo(header)
            snippet = " ".join(r["content"].split())
            if len(snippet) > snippet_chars:
                snippet = snippet[:snippet_chars].rsplit(" ", 1)[0] + "…"
            click.echo(f"> {snippet}")
            if r["url"]:
                click.echo(f"> *{r['url']} ({r['license']})*")
            click.echo()
        return

    for i, r in enumerate(results, 1):
        loc = " › ".join(p for p in (r["chapter"], r["section"]) if p)
        header = f"{i}. [{r['source']}] {r['title']}"
        if loc:
            header += f" — {loc}"
        click.echo(f"{header}  (score {r['score']:.2f})")
        snippet = " ".join(r["content"].split())
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars].rsplit(" ", 1)[0] + "…"
        click.echo(f"   {snippet}")
        if r["url"]:
            click.echo(f"   source: {r['url']} ({r['license']})")
        click.echo()


@main.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
@click.pass_context
def list_sources(ctx: click.Context, as_json: bool) -> None:
    """List indexed sources with chunk counts.

    \b
    Example:
        gptme-wisdom list
    """
    db: Path = ctx.obj["db"]
    if not db.exists():
        click.echo("(no wisdom index — run `gptme-wisdom ingest` first)")
        return

    with BookIndex(db_path=db) as idx:
        sources = idx.sources()

    if as_json:
        click.echo(json.dumps(sources, indent=2))
        return

    if not sources:
        click.echo("(index is empty)")
        return

    click.echo(f"{'Source':<16} {'Chunks':>6}  {'Title'}")
    click.echo("-" * 70)
    for s in sources:
        click.echo(f"{s['source']:<16} {s['chunks']:>6}  {s['title']}")


@main.command()
@click.argument("source")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remove(ctx: click.Context, source: str, yes: bool) -> None:
    """Remove all chunks for a source slug from the index.

    \b
    Example:
        gptme-wisdom remove sicp
    """
    db: Path = ctx.obj["db"]
    if not db.exists():
        click.echo("(no wisdom index)", err=True)
        sys.exit(1)

    with BookIndex(db_path=db) as idx:
        count = idx.count(source=source)
        if count == 0:
            click.echo(f"source '{source}' not found in index")
            return
        if not yes:
            click.confirm(f"Remove {count} chunks for '{source}'?", default=False, abort=True)
        removed = idx.remove_source(source)

    click.echo(f"removed {removed} chunks for '{source}'")


if __name__ == "__main__":
    main()
