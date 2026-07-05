# Command Catalog

Thin discoverability surface for reusable procedures that live in scripts, docs,
or package entrypoints.

This directory mirrors the command-entry format used in Bob's workspace:

- one Markdown file per command
- YAML frontmatter for machine-readable metadata
- the real behavior stays in the owning script or documentation

Keep entries thin. The goal is runtime discovery, not duplicating full docs.

## Entry Contract

Required frontmatter:

- `description`: short one-line summary
- `action.kind`: one of `shell`, `skill`, `doc`
- `action.run` for `shell`, `action.path` for `skill` and `doc`
- `owner_paths`: canonical implementation surfaces

Common optional fields:

- `when`: short discovery triggers
- `entrypoints`: where the command makes sense (`cli`, `agent`)
- `aliases`: alternate search names
- `parameters`: typed inputs/flags when needed
- `verification`: post-action checks

This repo does not yet ship a dedicated catalog CLI. For now, entries are meant to
be human-readable and easy for downstream agents/workspaces to parse or vendor.
