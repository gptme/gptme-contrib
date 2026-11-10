# Memory Boundary Auditor — Test Fixture

Slice 1.1 of `tasks/memory-boundary-auditor-5517.md`. This fixture exercises
the four memory-boundary categories and the layered-root conflict path the
auditor must handle.

## Layout

```text
workspace/
├── project-memory/
│   ├── core-goal.md          # durable_project_memory (canonical happy path)
│   └── archive-policy.md     # durable_project_memory (cold-but-durable)
├── transcript-snippet.md     # resumable_session_state (ephemeral)
├── secrets-fixture.md        # ambiguous_leakage (must be flagged, never persisted)
├── cache/
│   └── rag-results.md        # cache (regenerable, drop on migrate)
└── conflicting-roots/
    ├── project-layer.md      # name=shared-policy, layer=project
    └── user-layer.md         # name=shared-policy, layer=user (collision)
```

## Categories the auditor must distinguish

| Classification               | Test file                          | Expected action          |
|------------------------------|------------------------------------|--------------------------|
| `durable_project_memory`     | `project-memory/*.md`              | migrate_verbatim         |
| `resumable_session_state`    | `transcript-snippet.md`            | migrate_if_session_active |
| `cache`                      | `cache/rag-results.md`             | drop                     |
| `ambiguous_leakage`          | `secrets-fixture.md`               | refuse + warn            |

## Layered-root conflict

`conflicting-roots/project-layer.md` and `conflicting-roots/user-layer.md`
both declare `name: shared-policy`. The auditor must:

1. Report both files (no silent pick)
2. Attach provenance (`layer: project` vs `layer: user`)
3. Apply nearest-root resolution for the read path while preserving the
   conflict as durable information

## Verdict spec

The auditor's output for one classified file is defined in
[`verdict-spec.json`](verdict-spec.json). The spec is intentionally small:
`path`, `classification`, `confidence`, `rationale`, `provenance`,
`migration_preview`. Confidence below 0.6 must surface to the operator
rather than be silently classified.

## Out of scope for this fixture

- The actual auditor implementation (slice 2.1 of the parent task)
- The migration dry-run script (slice 2.2)
- Production wiring against `gptme-util memory` (slice 3.x)
