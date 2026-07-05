"""Allow ``python -m gptme_sessions`` to invoke the CLI."""

from .cli import main

raise SystemExit(main())
