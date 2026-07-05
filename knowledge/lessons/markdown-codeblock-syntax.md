# Markdown Codeblock Syntax (Companion)

Full reference for `lessons/tools/markdown-codeblock-syntax.md`.

The primary lesson is intentionally short because it is always included in some
agent prompt surfaces. This companion keeps the rationale and operational notes
out of the runtime lesson while preserving the details for maintainers.

## Failure Mode

Some agent file-write paths and transcript renderers become ambiguous when a
Markdown document contains bare triple-backtick fences. If generation stops
inside a fence, or if the surrounding message parser misidentifies where a fence
ends, the saved file can end up incomplete or structurally invalid.

The most visible symptoms are:
- a document ending immediately after a heading or label
- an unfinished fenced block near the end of a generated file
- follow-up instructions to append missing content
- markdown validators reporting unbalanced fences

## Recommended Pattern

Use explicit language tags for every fenced block:

````markdown
```txt
plain text
```

```csv
name,value
example,1
```

```shell
printf '%s\n' "example"
```
````

For nested Markdown examples, wrap the outer example in a longer fence:

`````markdown
````markdown
```txt
nested content
```
````
`````

## Keyword Design

The lesson keywords should describe failure observations rather than phrases in
the lesson body or always-loaded identity files. A keyword that appears in an
autoloaded prompt surface will fire the lesson on every session, hiding the real
signal and weakening lesson telemetry.

Good triggers are concrete phrases an agent is likely to say while diagnosing a
failed write. Avoid generic terms that appear in documentation about the lesson
itself.

## Verification

After editing this lesson, run:

```shell
python3 scripts/lesson-keyword-health.py --autoloaded-context
python3 packages/gptme-lessons-extras/src/gptme_lessons_extras/validate.py \
  lessons/tools/markdown-codeblock-syntax.md
prek run --files lessons/tools/markdown-codeblock-syntax.md
```

The health check should report no autoloaded-context hits for this lesson.

## Related

- Primary lesson: `lessons/tools/markdown-codeblock-syntax.md`
- Validator: `scripts/precommit/validators/validate_markdown_codeblock_syntax.py`
