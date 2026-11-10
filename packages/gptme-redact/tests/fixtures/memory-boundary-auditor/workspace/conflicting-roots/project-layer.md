---
name: shared-policy
description: "Project-layer version: stays narrow to project boundaries"
status: living
layer: project
---

# Shared Policy (project layer)

The project-layer version keeps the policy narrow — it does not affect
machine-wide behavior. The user-layer file in `../user-layer.md` has a
wider scope, and the auditor must resolve the conflict by NEAREST ROOT
(project > user for project-scoped reads).

This file is durable; its sibling user-layer file is durable too. The
auditor must report both with provenance and not silently pick one.
