import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from chromadb.api.types import Documents, EmbeddingFunction
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

OPENROUTER_EMBEDDINGS_ENDPOINT = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_OPENROUTER_EMBEDDING_MODEL = "openai/text-embedding-3-large"
DEFAULT_OPENROUTER_MAX_INPUTS_PER_REQUEST = 64
DEFAULT_OPENROUTER_MAX_TOKENS_PER_REQUEST = 24_000


class ModernBERTEmbedding(EmbeddingFunction):
    def __init__(self, model_name: str = "joe32140/ModernBERT-base-msmarco", device: str = "cpu"):
        """Initialize ModernBERT embedding function.

        Args:
            model_name: Name of the ModernBERT model to use. Options:
                - "joe32140/ModernBERT-base-msmarco" (default, optimized for retrieval)
                  Best for search/retrieval tasks, trained with contrastive learning on MS MARCO.
                  Recommended chunk size: 512-1024 tokens for general text, 256-512 for code.
                - "answerdotai/ModernBERT-base" (general purpose)
                  Better for tasks requiring deeper semantic understanding.
                  Can handle longer chunks (up to 8192 tokens).
            device: Device to run the model on (defaults to 'cpu')

        Note:
            The msmarco variant is specifically optimized for retrieval tasks and should give
            better results for search/similarity use cases. It works best with smaller chunk
            sizes as it's trained on passage-level data.
        """
        self.model_name = model_name
        self.is_msmarco = "msmarco" in model_name.lower()
        self.model = SentenceTransformer(model_name, device=device)

    def __call__(self, texts: Documents) -> list[list[float]]:  # type: ignore[override]
        """Generate embeddings for the input texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings
        """
        # Batch inputs for efficiency
        embeddings: list[list[float]] = self.model.encode(
            texts,
            batch_size=32,  # Adjust based on GPU memory
            convert_to_numpy=True,
            normalize_embeddings=True,  # Normalize for cosine similarity
        ).tolist()
        return embeddings


def resolve_openrouter_api_key() -> str | None:
    """Return an OpenRouter API key from the environment, if configured."""
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY_EVAL")


def _default_openrouter_cache_path() -> Path:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_root / "gptme-rag" / "openrouter-embeddings.sqlite"


class _SQLiteEmbeddingCache:
    """Small SQLite cache keyed by embedding model and content hash.

    Bounded by ``max_rows``: before each insert batch, the oldest entries (by
    ``accessed_at``) are pruned to make room so that freshly inserted rows are
    never evicted.  If the batch itself exceeds ``max_rows``, it is truncated to
    the last ``max_rows`` items before the prune/insert step.
    """

    DEFAULT_MAX_ROWS = 100_000  # ~3-4 GB at 3072-dim float32; safe default

    def __init__(self, path: Path, max_rows: int = DEFAULT_MAX_ROWS):
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "model TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, "
            "embedding_json TEXT NOT NULL, "
            "accessed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "PRIMARY KEY (model, content_hash)"
            ")"
        )
        # Migrate existing caches that lack the accessed_at column.
        try:
            self.conn.execute(
                "ALTER TABLE embeddings ADD COLUMN accessed_at TEXT NOT NULL DEFAULT '2000-01-01T00:00:00.000Z'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        self.conn.commit()
        self.lock = threading.Lock()

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    def get_many(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}

        found: dict[str, list[float]] = {}
        with self.lock:
            for offset in range(0, len(hashes), 500):
                chunk = list(dict.fromkeys(hashes[offset : offset + 500]))
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    "SELECT content_hash, embedding_json FROM embeddings "
                    f"WHERE model = ? AND content_hash IN ({placeholders})",
                    [model, *chunk],
                ).fetchall()
                for content_hash, embedding_json in rows:
                    found[content_hash] = json.loads(embedding_json)
        return found

    def put_many(self, model: str, items: list[tuple[str, list[float]]]) -> None:
        if not items:
            return
        # If the batch itself exceeds max_rows, keep only the last max_rows items.
        if len(items) > self.max_rows:
            items = items[-self.max_rows :]

        now = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        with self.lock:
            # Prune BEFORE inserting so freshly inserted rows always survive.
            # All rows in an executemany batch get the same accessed_at timestamp,
            # so a post-insert prune has no tiebreaker and can arbitrarily evict
            # just-inserted entries.  Pre-emptively free (len(items)) slots instead.
            row_count = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            target = self.max_rows - len(items)
            if row_count > target:
                excess = row_count - target
                self.conn.execute(
                    "DELETE FROM embeddings WHERE (model, content_hash) IN ("
                    "  SELECT model, content_hash FROM embeddings ORDER BY accessed_at ASC LIMIT ?"
                    ")",
                    (excess,),
                )
            self.conn.executemany(
                f"INSERT OR REPLACE INTO embeddings "
                f"(model, content_hash, embedding_json, accessed_at) VALUES (?, ?, ?, ({now}))",
                [(model, content_hash, json.dumps(vector)) for content_hash, vector in items],
            )
            self.conn.commit()


class OpenRouterEmbedding(EmbeddingFunction):
    """ChromaDB embedding function backed by OpenRouter's embeddings API."""

    is_msmarco = False

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        cache_path: Path | None = None,
        max_inputs_per_request: int = DEFAULT_OPENROUTER_MAX_INPUTS_PER_REQUEST,
        max_tokens_per_request: int = DEFAULT_OPENROUTER_MAX_TOKENS_PER_REQUEST,
    ):
        self.model_name = (
            model_name
            or os.environ.get("OPENROUTER_EMBEDDING_MODEL")
            or DEFAULT_OPENROUTER_EMBEDDING_MODEL
        )
        self.api_key = api_key or resolve_openrouter_api_key()
        if not self.api_key:
            raise ValueError(
                "OpenRouter embeddings require OPENROUTER_API_KEY; "
                "Indexer falls back to local embeddings when no key is configured"
            )
        self.cache = _SQLiteEmbeddingCache(cache_path or _default_openrouter_cache_path())
        self.max_inputs_per_request = max_inputs_per_request
        self.max_tokens_per_request = max_tokens_per_request
        self.stats = {
            "requests": 0,
            "api_texts": 0,
            "cached_texts": 0,
            "tokens": 0,
            "cost": 0.0,
        }

    @staticmethod
    def name() -> str:
        """Return a stable ChromaDB embedding-function name."""
        return "openrouter"

    def __call__(self, input: Documents) -> list[list[float]]:  # type: ignore[override]
        """Generate embeddings for the input texts."""
        texts = [text if text.strip() else " " for text in input]
        if not texts:
            return []

        hashes = [_SQLiteEmbeddingCache.content_hash(text) for text in texts]
        cached = self.cache.get_many(self.model_name, list(dict.fromkeys(hashes)))
        self.stats["cached_texts"] += sum(1 for content_hash in hashes if content_hash in cached)

        missing = [
            (i, texts[i]) for i, content_hash in enumerate(hashes) if content_hash not in cached
        ]
        fresh: list[tuple[str, list[float]]] = []
        for batch in self._make_batches(missing):
            vectors = self._embed_batch([text for _i, text in batch])
            for (idx, _text), vector in zip(batch, vectors):
                cached[hashes[idx]] = vector
                fresh.append((hashes[idx], vector))

        self.cache.put_many(self.model_name, fresh)
        return [cached[content_hash] for content_hash in hashes]

    @staticmethod
    def _approx_tokens(text: str) -> int:
        return max(1, len(text) // 2)

    def _make_batches(self, items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
        batches: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        current_tokens = 0

        for idx, text in items:
            token_count = self._approx_tokens(text)
            if token_count > self.max_tokens_per_request:
                raise ValueError(
                    f"Text at index {idx} is too large (~{token_count} tokens) for the OpenRouter "
                    f"embeddings API (max {self.max_tokens_per_request} tokens per request). "
                    f"Reduce --chunk-size to avoid this error."
                )
            if current and (
                len(current) >= self.max_inputs_per_request
                or current_tokens + token_count > self.max_tokens_per_request
            ):
                batches.append(current)
                current = []
                current_tokens = 0
            current.append((idx, text))
            current_tokens += token_count

        if current:
            batches.append(current)
        return batches

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model_name, "input": texts}).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(6):
            request = urllib.request.Request(
                OPENROUTER_EMBEDDINGS_ENDPOINT,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    data = json.loads(response.read())
                if "error" in data:
                    raise RuntimeError(f"OpenRouter embeddings error: {data['error']}")

                rows = sorted(data["data"], key=lambda row: row["index"])
                if len(rows) != len(texts):
                    raise RuntimeError(
                        f"OpenRouter embeddings API returned {len(rows)} vectors for "
                        f"{len(texts)} input texts — malformed or partial response."
                    )
                actual_indices = [row["index"] for row in rows]
                if actual_indices != list(range(len(texts))):
                    raise RuntimeError(
                        f"OpenRouter embeddings API returned non-contiguous indices {actual_indices!r}; "
                        f"expected {list(range(len(texts)))} — malformed response, embeddings "
                        "would be assigned to the wrong texts."
                    )
                usage = data.get("usage") or {}
                self.stats["requests"] += 1
                self.stats["api_texts"] += len(texts)
                self.stats["tokens"] += int(usage.get("total_tokens") or 0)
                self.stats["cost"] += float(usage.get("cost") or 0.0)
                return [_l2_normalize(row["embedding"]) for row in rows]
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = exc.read()[:300].decode("utf-8", "replace")
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise RuntimeError(f"OpenRouter embeddings HTTP {exc.code}: {body}") from exc
                logger.warning("OpenRouter embeddings HTTP %s; retrying", exc.code)
                time.sleep(min(60, 2**attempt) + 0.5 * attempt)
            except RuntimeError:
                # RuntimeErrors are permanent (e.g. bad model name, API-level error,
                # or count mismatch above) — do not retry.
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("OpenRouter embeddings request failed; retrying: %r", exc)
                time.sleep(min(60, 2**attempt))

        raise RuntimeError(f"OpenRouter embeddings failed after retries: {last_error!r}")


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class GenericSentenceTransformerEmbedding(EmbeddingFunction):
    """Generic embedding function for any sentence-transformers model."""

    def __init__(self, model_name: str, device: str = "cpu"):
        """Initialize with any sentence-transformers model.

        Args:
            model_name: Hugging Face model name (e.g., "all-MiniLM-L6-v2", "all-mpnet-base-v2")
            device: Device to run the model on (defaults to 'cpu')
        """
        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device=device)

    def __call__(self, texts: Documents) -> list[list[float]]:  # type: ignore[override]
        """Generate embeddings for the input texts."""
        embeddings: list[list[float]] = self.model.encode(
            texts,
            batch_size=32,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).tolist()
        return embeddings
