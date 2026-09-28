---
name: journal
description: Create or backfill append-only agent journal entries under the day the work happened, especially when writing after midnight or reconstructing a delayed session from timestamps.
---

# Journal

Journal entries belong to the work day, not automatically to the day they are
written. Preserve each agent's local filename, metadata, and body format; this
skill standardizes only dating and append-only history.

## Choose the date

1. Use the date on which the recorded work began or primarily happened.
2. For delayed write-ups, verify the date from session-start, commit, or tool
   timestamps. Do not infer it from relative words such as "yesterday".
3. If the date cannot be recovered reliably, use the write date and disclose
   that uncertainty in the entry.

Include both the work start and write time when the entry is written later.
Keep the repository's existing filename and metadata conventions; do not
replace topic names, session identifiers, or local frontmatter with a shared
template.

## Create without overwriting

The bundled helper creates `journal/YYYY-MM-DD/<name>.md`, refuses an existing
path, and writes stdin verbatim:

```bash
printf '%s\n' '# Session notes' | \
  JOURNAL_STARTED='2026-09-26T21:30:00+02:00' \
  bash "$SKILL_DIR/scripts/journal-new.sh" erbot-session
```

Use `JOURNAL_DATE=YYYY-MM-DD` when only the work day is known. Set
`JOURNAL_ROOT` only when the journal is outside the current Git repository.
If both `JOURNAL_DATE` and `JOURNAL_STARTED` are set, their dates must agree.

## Preserve history

- A new file in a past-dated directory is valid.
- Existing historical entries are append-only.
- Do not replace committed lines, including mutable-looking frontmatter.
- Record a later correction in a new entry on the correction day and link the
  original claim.
