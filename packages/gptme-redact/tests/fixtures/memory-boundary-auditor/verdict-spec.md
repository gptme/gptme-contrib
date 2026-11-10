# Memory Boundary Verdict — JSON Schema

Verdict emitted by the memory boundary auditor for one classified file in the
fixture workspace. The auditor's job is **read-only**: classify, attribute,
preview. It never moves or persists anything.

The machine-readable copy of this schema is
[`verdict-spec.json`](verdict-spec.json); this document is the prose
explanation and rationale. The two are kept in sync — the JSON block below is
the source extracted into that file.

The schema below is the canonical contract. The auditor's output for one
classified file MUST conform to this schema. Slice 2.1 of
`tasks/memory-boundary-auditor-5517.md` is the implementation; this file is
the spec it must satisfy.

## JSON Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Memory Boundary Verdict",
  "description": "Verdict emitted by the memory boundary auditor for one classified file in the fixture workspace. The auditor's job is read-only: classify, attribute, preview. It never moves or persists anything.",
  "type": "object",
  "required": ["path", "classification", "confidence", "rationale", "provenance", "migration_preview"],
  "properties": {
    "path": {
      "type": "string",
      "description": "Relative path of the classified file inside the fixture workspace."
    },
    "classification": {
      "type": "string",
      "enum": [
        "durable_project_memory",
        "resumable_session_state",
        "cache",
        "ambiguous_leakage"
      ],
      "description": "The boundary category. Migration policy: durable -> persist verbatim; resumable -> persist only if gated on active session; cache -> drop; ambiguous_leakage -> refuse to migrate, surface to operator."
    },
    "confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "Auditor's confidence in the classification. Below 0.6 the file should be surfaced to the operator, not silently classified."
    },
    "rationale": {
      "type": "string",
      "description": "One-sentence justification citing the rule that fired (frontmatter markers, content shape, layer, or credential-like pattern)."
    },
    "provenance": {
      "type": "object",
      "required": ["layer", "name"],
      "properties": {
        "layer": {
          "type": "string",
          "enum": ["project", "user", "agent", "explicit", "unknown"],
          "description": "Which memory layer the file belongs to. Layered-root resolution: nearest layer wins on name collision, but the conflict itself is reported."
        },
        "name": {
          "type": "string",
          "description": "Memory entry name from frontmatter, or basename if no frontmatter."
        },
        "supersedes": {
          "type": ["string", "null"],
          "description": "Name of an entry this one supersedes, if any."
        }
      }
    },
    "migration_preview": {
      "type": "object",
      "required": ["action", "target_layer"],
      "properties": {
        "action": {
          "type": "string",
          "enum": ["migrate_verbatim", "migrate_if_session_active", "drop", "refuse"],
          "description": "What the migration dry-run would do, never the actual migration."
        },
        "target_layer": {
          "type": ["string", "null"],
          "description": "Destination memory layer if action != drop/refuse. null for drop/refuse."
        },
        "warnings": {
          "type": "array",
          "items": {"type": "string"},
          "description": "Free-form warnings (e.g. 'contains credential-like content', 'name collision with user-layer entry')."
        }
      }
    }
  },
  "examples": [
    {
      "path": "workspace/project-memory/core-goal.md",
      "classification": "durable_project_memory",
      "confidence": 0.95,
      "rationale": "Has name/description frontmatter, status=living, type=project, no session markers.",
      "provenance": {"layer": "project", "name": "core-goal", "supersedes": null},
      "migration_preview": {"action": "migrate_verbatim", "target_layer": "project", "warnings": []}
    },
    {
      "path": "workspace/secrets-fixture.md",
      "classification": "ambiguous_leakage",
      "confidence": 0.99,
      "rationale": "Filename and contents match credential-like pattern (api_key=, github_pat=, *_key=).",
      "provenance": {"layer": "unknown", "name": "secrets-fixture", "supersedes": null},
      "migration_preview": {"action": "refuse", "target_layer": null, "warnings": ["credential-like content", "would be persisted if not flagged"]}
    }
  ]
}
```

## Why this contract

- **Read-only invariant**: the `migration_preview.action` is a preview verb
  (`migrate_verbatim`, `migrate_if_session_active`, `drop`, `refuse`), never
  `migrate_now`. The auditor never persists.
- **Confidence is mandatory**: a verdict without `confidence` cannot be
  reasoned about. Below 0.6 must surface to the operator, not silently
  classify.
- **Layered-root provenance**: every verdict carries `provenance.layer` so
  the dry-run can apply nearest-root resolution on `conflicting-roots/`
  fixtures without losing the conflict itself.
- **Refuse for leakage**: `ambiguous_leakage` always maps to `refuse`,
  never `drop` or `migrate_verbatim`. A silent persistence of a credential
  is the exact failure the auditor exists to prevent.
