"""TF-IDF lexical retrieval backend for gptme-rag.

The dense (embedding) path is measurably weak on exact-identifier queries: a
function name, a file path, or an error string is a rare term that semantic
embeddings drop.  A lexical backend preserves those rare terms and is a
required complement for canonical retrieval — see the 2026-08-10 dense-vs-lexical
trial for the measurement.

This module provides a thin wrapper over scikit-learn's
:class:`~sklearn.feature_extraction.text.TfidfVectorizer` plus cosine-similarity
ranking, with a relevance floor and path exclusion so it can slot into the same
query surface as the dense :class:`~gptme_rag.indexing.indexer.Indexer`.

scikit-learn is an optional dependency (``pip install gptme-rag[lexical]``)
so dense-only users do not pay its install cost.  Building or querying an index
without scikit-learn installed raises :class:`LexicalDependencyMissing`.
"""

from __future__ import annotations

import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .indexing.document import Document

logger = logging.getLogger(__name__)


class LexicalDependencyMissing(ImportError):
    """Raised when the lexical backend is used without scikit-learn installed."""


@dataclass
class LexicalHit:
    """A ranked lexical retrieval result."""

    document: Document
    score: float
    rank: int


class TfidfIndex:
    """A persistent TF-IDF index over :class:`Document` objects.

    The default vectorizer settings are deliberately uncapped
    (``max_features=None``) because a global top-N vocabulary is chosen by
    corpus-wide frequency, so rare terms — exactly the identifiers lexical
    retrieval exists to find — get dropped first.  See the build-time comment in
    the Bob brain script for the measurement behind this default.
    """

    def __init__(
        self,
        *,
        stop_words: str | list[str] | None = "english",
        ngram_range: tuple[int, int] = (1, 2),
        sublinear_tf: bool = True,
        max_features: int | None = None,
        relevance_floor: float = 0.05,
    ):
        self.stop_words = stop_words
        self.ngram_range = ngram_range
        self.sublinear_tf = sublinear_tf
        self.max_features = max_features
        self.relevance_floor = relevance_floor
        self._vectorizer = None
        self._matrix = None
        self._documents: list[Document] = []

    # -- build ------------------------------------------------------------

    def index(self, documents: list[Document]) -> None:
        """Fit the vectorizer over ``documents`` and store the document list.

        Passing an empty list resets the index to an unbuilt state so that a
        subsequent :meth:`search` returns ``[]`` rather than crashing.
        """
        if not documents:
            self._vectorizer = None
            self._matrix = None
            self._documents = []
            return
        vectorizer = self._make_vectorizer()
        texts = [doc.content for doc in documents]
        self._matrix = vectorizer.fit_transform(texts)
        self._vectorizer = vectorizer
        self._documents = list(documents)

    def _make_vectorizer(self):
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LexicalDependencyMissing(
                "scikit-learn is required for lexical retrieval; "
                "install it with `pip install gptme-rag[lexical]`"
            ) from exc

        return TfidfVectorizer(
            max_features=self.max_features,
            stop_words=self.stop_words,
            ngram_range=self.ngram_range,
            sublinear_tf=self.sublinear_tf,
        )

    # -- query ------------------------------------------------------------

    def search(
        self,
        query: str,
        n_results: int = 5,
        exclude_paths: set[str] | None = None,
    ) -> list[LexicalHit]:
        """Return up to ``n_results`` hits ranked by cosine similarity.

        ``exclude_paths`` is a set of source paths to skip (e.g. documents already
        in context).  Hits below ``relevance_floor`` are discarded.
        """
        if self._vectorizer is None or self._matrix is None:
            return []

        try:
            from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LexicalDependencyMissing(
                "scikit-learn is required for lexical retrieval; "
                "install it with `pip install gptme-rag[lexical]`"
            ) from exc

        q_vec = self._vectorizer.transform([query])
        sims = cosine_similarity(q_vec, self._matrix)[0]
        excluded = exclude_paths or set()

        hits: list[LexicalHit] = []
        for rank, idx in enumerate(sims.argsort()[::-1]):
            score = float(sims[idx])
            if score < self.relevance_floor:
                continue
            doc = self._documents[idx]
            if self._source_path(doc) in excluded:
                continue
            hits.append(LexicalHit(document=doc, score=round(score, 4), rank=rank))
            if len(hits) >= n_results:
                break
        return hits

    @staticmethod
    def _source_path(doc: Document) -> str:
        if doc.source_path is not None:
            return str(doc.source_path)
        return str(doc.metadata.get("source", ""))

    # -- persistence ------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist the fitted index atomically to ``path``."""
        if self._vectorizer is None or self._matrix is None:
            raise ValueError("Cannot save an index that has not been built")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "vectorizer": self._vectorizer,
            "matrix": self._matrix,
            "documents": self._documents,
            "stop_words": self.stop_words,
            "ngram_range": self.ngram_range,
            "sublinear_tf": self.sublinear_tf,
            "max_features": self.max_features,
            "relevance_floor": self.relevance_floor,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as f:
            pickle.dump(payload, f)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> TfidfIndex:
        """Load a previously saved index.

        .. warning::
            The index file is deserialized with :mod:`pickle`.  Only load
            files that were written by :meth:`save` on this machine or by a
            trusted process.  Never load an index obtained from a network
            location, an untrusted archive, or an unknown source — a crafted
            pickle payload can execute arbitrary code.
        """
        path = Path(path)
        with open(path, "rb") as f:
            payload = pickle.load(f)  # nosec B301 — trusted-file; see docstring
        idx = cls(
            stop_words=payload["stop_words"],
            ngram_range=payload["ngram_range"],
            sublinear_tf=payload["sublinear_tf"],
            max_features=payload["max_features"],
            relevance_floor=payload.get("relevance_floor", 0.05),
        )
        idx._vectorizer = payload["vectorizer"]
        idx._matrix = payload["matrix"]
        idx._documents = payload["documents"]
        return idx


__all__ = ["TfidfIndex", "LexicalHit", "LexicalDependencyMissing"]
