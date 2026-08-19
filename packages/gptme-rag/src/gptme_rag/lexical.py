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

import io
import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .indexing.document import Document
from .memory_type import SUPPORTED_MEMORY_TYPES, weighted_similarity

logger = logging.getLogger(__name__)

# Modules whose classes are permitted during index deserialization.
# Limiting to these prevents arbitrary code execution from a crafted pickle.
_SAFE_MODULES = frozenset(
    {
        "collections",
        "datetime",
        "pathlib",
        "pathlib._local",  # Python 3.13+ internal module for PosixPath/WindowsPath
        "gptme_rag",
        "gptme_rag.indexing.document",
        # sklearn and its internal modules (needed for TfidfVectorizer)
        "sklearn",
        "sklearn.feature_extraction",
        "sklearn.feature_extraction.text",
        "sklearn.feature_extraction._stop_words",
        "sklearn.pipeline",
        "sklearn.utils",
        "sklearn.utils._bunch",
        "sklearn.utils._tags",
        "sklearn.utils.validation",
        # scipy sparse matrices (persisted inside TfidfVectorizer state)
        "scipy",
        "scipy.sparse",
        "scipy.sparse._csr",
        "scipy.sparse._csc",
        "scipy.sparse._compressed",
        "scipy.sparse._data_matrix",
        "scipy.sparse._base",
        "scipy.sparse._index",
        # numpy arrays and dtypes (numpy ≥2.0 uses numpy._core instead of numpy.core)
        "numpy",
        "numpy.core",
        "numpy.core.multiarray",
        "numpy.dtypes",
        "numpy._core",
        "numpy._core.multiarray",
        "numpy._core._multiarray_umath",
    }
)

# Explicit allowlist of builtins pickle may reference for basic Python types.
# Broad "builtins" top-level permission would also admit eval/exec/open/compile.
_SAFE_BUILTINS = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "list",
        "tuple",
        "dict",
        "set",
        "frozenset",
        "complex",
        "slice",
        "type",
        "object",
        "NoneType",
    }
)


class _RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that only allows classes from known-safe modules."""

    def find_class(self, module: str, name: str):
        if module == "builtins":
            if name in _SAFE_BUILTINS:
                return super().find_class(module, name)
            raise pickle.UnpicklingError(f"Blocked builtin: {module}.{name}")
        if module in _SAFE_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked class: {module}.{name}")


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
        try:
            self._matrix = vectorizer.fit_transform(texts)
        except ValueError:
            # All texts reduced to empty vocabulary (e.g. all stop words).
            self._vectorizer = None
            self._matrix = None
            self._documents = []
            return
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
        memory_types: set[str] | None = None,
    ) -> list[LexicalHit]:
        """Return up to ``n_results`` hits ranked by cosine similarity.

        ``exclude_paths`` is a set of source paths to skip (e.g. documents already
        in context).  Hits below ``relevance_floor`` are discarded.

        ``memory_types`` is an optional set of memory-type labels (e.g.
        ``{"goal", "identity"}``) that boosts matching documents and penalises
        non-matching ones via :func:`~gptme_rag.memory_type.weighted_similarity`.
        Documents must carry a ``"memory_type"`` key in their metadata (set by the
        caller during indexing) for this to take effect; documents without the key
        are ranked by raw cosine similarity.  Only types in
        :data:`~gptme_rag.memory_type.SUPPORTED_MEMORY_TYPES` are considered —
        unrecognised values are silently ignored.
        """
        if n_results <= 0:
            return []
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
        raw_sims = cosine_similarity(q_vec, self._matrix)[0]
        excluded = exclude_paths or set()

        # Normalise the requested memory types to only known values so that a
        # typo in the caller's set does not silently ignore all boosts.
        requested: set[str] | None = None
        if memory_types:
            requested = {t for t in memory_types if t in SUPPORTED_MEMORY_TYPES}
            if not requested:
                requested = None

        # Compute weighted scores and sort by them so that boosted documents
        # rank above documents with a higher raw similarity but wrong type.
        def _weighted(idx: int) -> float:
            doc = self._documents[idx]
            mem_type: str | None = doc.metadata.get("memory_type") or None
            # Treat unrecognised memory-type labels as untagged (no penalty).
            if mem_type not in SUPPORTED_MEMORY_TYPES:
                mem_type = None
            return weighted_similarity(float(raw_sims[idx]), mem_type, requested)

        sorted_indices = sorted(range(len(raw_sims)), key=_weighted, reverse=True)

        hits: list[LexicalHit] = []
        for rank, idx in enumerate(sorted_indices):
            score = _weighted(idx)
            if score < self.relevance_floor:
                break  # sorted descending — nothing below this will clear the floor
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

        Deserialization uses :class:`_RestrictedUnpickler`, which only
        instantiates classes from known-safe modules (builtins, numpy, scipy,
        sklearn, gptme_rag).  Crafted pickles containing arbitrary classes are
        rejected with :class:`pickle.UnpicklingError`.
        """
        path = Path(path)
        with open(path, "rb") as f:
            data = f.read()
        try:
            payload = _RestrictedUnpickler(io.BytesIO(data)).load()
        except ImportError as exc:
            raise LexicalDependencyMissing(
                "scikit-learn is required to load a lexical index; "
                "install it with `pip install gptme-rag[lexical]`"
            ) from exc
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
