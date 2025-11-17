"""Smart caching system for RAG search results.

This module implements an LRU cache with TTL support for caching RAG search results.
It includes:
- CacheKey: Composite key structure for search queries
- CacheEntry: Cached results with metadata
- SmartRAGCache: Thread-safe LRU cache with memory management

Design: knowledge/technical-designs/rag-smart-caching-design.md
"""

import hashlib
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from threading import Lock


@dataclass(frozen=True)
class CacheKey:
    """Composite key for caching RAG search results.

    Attributes:
        query_text: Original search query (for debugging)
        query_hash: MD5 hash of normalized query
        paths_hash: Hash of paths list (None if not specified)
        n_results: Number of results requested
        where_hash: Hash of where clause dict (None if not specified)
        group_chunks: Whether chunks are grouped by document
        path_filters_hash: Hash of path filter patterns (None if not specified)
        embedding_model: Which embedding model used
        index_version: Index format/structure version
    """

    query_text: str
    query_hash: str
    paths_hash: str | None
    n_results: int
    where_hash: str | None
    group_chunks: bool
    path_filters_hash: str | None
    embedding_model: str
    index_version: str

    def __hash__(self) -> int:
        """Enable use as dict key."""
        return hash(
            (
                self.query_hash,
                self.paths_hash,
                self.n_results,
                self.where_hash,
                self.group_chunks,
                self.path_filters_hash,
                self.embedding_model,
                self.index_version,
            )
        )

    @classmethod
    def from_search(
        cls,
        query: str,
        paths: list | None = None,
        n_results: int = 5,
        where: dict | None = None,
        group_chunks: bool = True,
        path_filters: tuple | None = None,
        embedding_model: str = "modernbert",
        index_version: str = "v1",
    ) -> "CacheKey":
        """Factory method for creating keys from search parameters.

        Args:
            query: Search query text
            paths: List of paths to search within
            n_results: Maximum number of results to return
            where: Additional where clauses for ChromaDB query
            group_chunks: Whether to group chunks from the same document
            path_filters: Glob patterns to filter documents by path
            embedding_model: Name of embedding model used
            index_version: Version of index format

        Returns:
            CacheKey instance with normalized query hash
        """
        # Normalize query (lowercase, strip whitespace)
        normalized = query.lower().strip()
        query_hash = hashlib.md5(normalized.encode()).hexdigest()

        # Create hash for paths
        paths_hash = None
        if paths is not None:
            paths_str = ",".join(sorted(str(p) for p in paths))
            paths_hash = hashlib.md5(paths_str.encode()).hexdigest()

        # Create hash for where clause
        where_hash = None
        if where is not None:
            where_str = str(sorted(where.items()))
            where_hash = hashlib.md5(where_str.encode()).hexdigest()

        # Create hash for path_filters
        path_filters_hash = None
        if path_filters is not None:
            filters_str = ",".join(sorted(path_filters))
            path_filters_hash = hashlib.md5(filters_str.encode()).hexdigest()

        return cls(
            query_text=query,
            query_hash=query_hash,
            paths_hash=paths_hash,
            n_results=n_results,
            where_hash=where_hash,
            group_chunks=group_chunks,
            path_filters_hash=path_filters_hash,
            embedding_model=embedding_model,
            index_version=index_version,
        )


@dataclass
class CacheEntry:
    """Cached search results with metadata.

    Attributes:
        document_contents: Full document text content
        document_metadatas: Document metadata dicts
        document_ids: Document IDs for tracking
        distances: Embedding distance scores
        created_at: When entry was cached
        last_accessed: Last access time (for LRU)
        access_count: Number of times accessed
        workspace_mtime: Workspace modification time
        index_mtime: Index file modification time
        embedding_time_ms: How long search took
        result_count: Number of results
    """

    # Results (full data for cache hit)
    document_contents: list[str]
    document_metadatas: list[dict]
    document_ids: list[str]
    distances: list[float]

    # Metadata
    created_at: datetime
    last_accessed: datetime
    access_count: int

    # Freshness indicators
    workspace_mtime: float
    index_mtime: float

    # Quality metrics
    embedding_time_ms: float
    result_count: int

    def is_fresh(self, ttl_seconds: int = 300) -> bool:
        """Check if entry is still valid.

        Args:
            ttl_seconds: Time-to-live in seconds (default: 5 minutes)

        Returns:
            True if entry age is less than TTL
        """
        age_seconds = (datetime.now() - self.created_at).total_seconds()
        return age_seconds < ttl_seconds

    def is_hot(self, threshold: int = 5) -> bool:
        """Check if frequently accessed (for background refresh).

        Args:
            threshold: Minimum access count to be considered hot

        Returns:
            True if access count exceeds threshold
        """
        return self.access_count >= threshold

    def size_bytes(self) -> int:
        """Estimate memory usage for LRU eviction.

        Returns:
            Approximate size in bytes
        """
        # Rough estimate: IDs + scores + metadata
        return (
            sum(sys.getsizeof(doc_id) for doc_id in self.document_ids)
            + sys.getsizeof(self.distances)
            + sys.getsizeof(self.created_at)
            + sys.getsizeof(self.last_accessed)
            + sys.getsizeof(self.workspace_mtime)
            + 200  # Metadata overhead
        )


class SmartRAGCache:
    """Thread-safe LRU cache with TTL and memory management.

    Features:
    - LRU eviction using OrderedDict
    - TTL-based expiry (default: 5 minutes)
    - Memory-aware eviction (default: 100MB)
    - Thread-safe operations
    - Statistics tracking

    Attributes:
        ttl_seconds: Time-to-live for cache entries
        max_memory_bytes: Maximum memory usage
        cache: OrderedDict storing cache entries
        lock: Thread lock for concurrent access
        stats: Cache statistics (hits, misses, evictions)
    """

    def __init__(
        self,
        ttl_seconds: int = 300,
        max_memory_bytes: int = 100 * 1024 * 1024,  # 100MB
    ):
        """Initialize cache.

        Args:
            ttl_seconds: Time-to-live in seconds (default: 5 minutes)
            max_memory_bytes: Maximum memory in bytes (default: 100MB)
        """
        self.ttl_seconds = ttl_seconds
        self.max_memory_bytes = max_memory_bytes

        self.cache: OrderedDict[CacheKey, CacheEntry] = OrderedDict()
        self.lock = Lock()

        # Statistics
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "ttl_evictions": 0,
            "memory_evictions": 0,
            "total_size_bytes": 0,
        }

    def get(self, key: CacheKey) -> CacheEntry | None:
        """Get cached entry if exists and fresh.

        Args:
            key: Cache key to look up

        Returns:
            CacheEntry if found and fresh, None otherwise
        """
        with self.lock:
            entry = self.cache.get(key)

            if entry is None:
                self.stats["misses"] += 1
                return None

            # Check TTL
            if not entry.is_fresh(self.ttl_seconds):
                self.stats["ttl_evictions"] += 1
                self.stats["misses"] += 1
                del self.cache[key]
                return None

            # Update LRU
            self.cache.move_to_end(key)
            entry.last_accessed = datetime.now()
            entry.access_count += 1

            self.stats["hits"] += 1
            return entry

    def put(self, key: CacheKey, entry: CacheEntry) -> None:
        """Store entry in cache with memory-aware eviction.

        Args:
            key: Cache key
            entry: Cache entry to store
        """
        with self.lock:
            # Remove existing entry if present
            if key in self.cache:
                old_entry = self.cache[key]
                self.stats["total_size_bytes"] -= old_entry.size_bytes()
                del self.cache[key]

            # Add new entry
            self.cache[key] = entry
            self.stats["total_size_bytes"] += entry.size_bytes()

            # Evict old entries if over memory limit
            while (
                self.stats["total_size_bytes"] > self.max_memory_bytes
                and len(self.cache) > 1
            ):
                # Remove least recently used (FIFO from OrderedDict)
                old_key, old_entry = self.cache.popitem(last=False)
                self.stats["total_size_bytes"] -= old_entry.size_bytes()
                self.stats["evictions"] += 1
                self.stats["memory_evictions"] += 1

    def clear(self) -> None:
        """Clear all cache entries."""
        with self.lock:
            self.cache.clear()
            self.stats["total_size_bytes"] = 0

    def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        with self.lock:
            total_requests = self.stats["hits"] + self.stats["misses"]
            hit_rate = (
                self.stats["hits"] / total_requests if total_requests > 0 else 0.0
            )

            return {
                **self.stats,
                "entries": len(self.cache),
                "hit_rate": hit_rate,
                "memory_mb": self.stats["total_size_bytes"] / (1024 * 1024),
            }

    def get_hot_keys(self, threshold: int = 5) -> list[CacheKey]:
        """Get frequently accessed keys for background refresh.

        Args:
            threshold: Minimum access count to be considered hot

        Returns:
            List of hot cache keys
        """
        with self.lock:
            return [key for key, entry in self.cache.items() if entry.is_hot(threshold)]
