---
name: shared-policy
description: "User-layer version: same name, broader scope"
status: living
layer: user
---

# Shared Policy (user layer)

The user-layer version of `shared-policy` has a broader scope than the
project-layer sibling. The auditor must:
1. Report BOTH files (no silent pick)
2. Attach provenance (layer=project vs layer=user)
3. Apply layered-root resolution: NEAREST root wins for the read path,
   but the conflict itself is durable information

This is durable project memory for both — the auditor should NOT classify
the conflict as cache or session-state.
