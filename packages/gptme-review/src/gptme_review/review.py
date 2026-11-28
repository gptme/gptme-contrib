"""Peer entry point for one PR review: ``python -m gptme_review.review OWNER/REPO N``.

Writer-port slice 3c. The reviewer identity comes from
``AI_REVIEW_REVIEWER_LOGIN`` and ``AI_REVIEW_MARKER_NAMESPACE``; both are
required, because there is no package-default identity to post under.
Routing is OpenRouter-only with the package REVIEW key, and the advertised
maintainer commands are review-only (:func:`gptme_review.writer.default_io`).
Hosts with a subscription panel, agent engine, verifier or ledgers keep their
own entry point and bind :mod:`gptme_review.writer` with their own namespace.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from gptme_review import lifecycle, writer
from gptme_review.sweep import sweep_identity

LOGIN_ENV = "AI_REVIEW_REVIEWER_LOGIN"
NAMESPACE_ENV = "AI_REVIEW_MARKER_NAMESPACE"


def main(argv: Sequence[str] | None = None) -> int:
    login = os.environ.get(LOGIN_ENV, "").strip()
    namespace = os.environ.get(NAMESPACE_ENV, "").strip()
    if not login or not namespace:
        print(
            f"error: set {LOGIN_ENV} and {NAMESPACE_ENV}; "
            "there is no default reviewer identity",
            file=sys.stderr,
        )
        return 2
    try:
        identity = sweep_identity(login, namespace)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    bound = writer.bind_io(writer.default_io(identity))
    with lifecycle.lifecycle_context() as run:
        result: int = bound.review_main(argv, description=__doc__)
        run.finish("completed" if result == 0 else "failed", exit_code=result)
        return result


if __name__ == "__main__":
    sys.exit(main())
