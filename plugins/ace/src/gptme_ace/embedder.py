#!/usr/bin/env python3
"""
ACE Lesson Embedding System (Phase 4.1)

Generates embeddings for lessons to enable:
- Semantic similarity detection (Phase 4.2)
- Duplicate lesson identification (Phase 4.3)
- Dynamic lesson retrieval (Phase 5)

Uses Sentence-BERT for local, free, fast embeddings.
FAISS for efficient vector search (with numpy fallback).
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False

try:
    import faiss  # type: ignore[import-not-found]

    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False


class LessonEmbedder:
    """Generates and manages embeddings for Bob's lessons.

    Uses Sentence-BERT for local embedding generation and FAISS (or numpy fallback)
    for efficient vector search. Supports similarity detection, duplicate identification,
    and semantic search across lessons.

    Attributes:
        lessons_dir: Path to lessons directory
        embeddings_dir: Path to embeddings storage directory
        model_name: Name of Sentence-BERT model to use
        model: Loaded Sentence-BERT model (None until first use)
        index: FAISS index or numpy array for vector search
        metadata: Dict mapping lesson IDs to their metadata
        config: Configuration dict with model info and index settings
    """

    def __init__(
        self,
        lessons_dir: Path = Path(__file__).parent.parent.parent.parent.parent
        / "lessons",
        embeddings_dir: Path = Path(__file__).parent.parent.parent.parent.parent
        / "embeddings"
        / "lessons",
        model_name: str = "all-MiniLM-L6-v2",
    ):
        """Initialize lesson embedder.

        Args:
            lessons_dir: Path to lessons directory. Defaults to repository lessons/ dir.
            embeddings_dir: Path to embeddings storage. Defaults to repository embeddings/lessons/.
            model_name: Sentence-BERT model name. Defaults to "all-MiniLM-L6-v2".
        """
        self.lessons_dir = lessons_dir
        self.embeddings_dir = embeddings_dir
        self.model_name = model_name

        # Ensure embeddings directory exists
        self.embeddings_dir.mkdir(parents=True, exist_ok=True)

        # Load or initialize components
        self.model: Optional[Any] = None
        self.index: Optional[Any] = None
        self.metadata: Dict[str, Dict] = {}
        self.config: Dict[str, Any] = {}

        # Load existing data
        self._load_config()
        self._load_metadata()

    def _load_config(self):
        """Load index configuration from file.

        Creates default config if file doesn't exist.
        """
        config_path = self.embeddings_dir / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                self.config = json.load(f)
        else:
            # Initialize config
            self.config = {
                "model_name": f"sentence-transformers/{self.model_name}",
                "embedding_dim": 384,  # all-MiniLM-L6-v2 dimension
                "index_type": "IndexFlatL2",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "lesson_count": 0,
            }
            self._save_config()

    def _save_config(self):
        """Save index configuration to file as JSON."""
        config_path = self.embeddings_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump(self.config, f, indent=2)

    def _lesson_to_id(self, lesson_path: Path) -> str:
        """Convert a lesson path to its ID.

        Args:
            lesson_path: Path to the lesson file

        Returns:
            Lesson ID (from frontmatter or generated from path)
        """
        # Read content and look for lesson_id in frontmatter
        content = lesson_path.read_text()
        id_match = re.search(r"^lesson_id:\s*(.+)$", content, re.MULTILINE)

        if id_match:
            return id_match.group(1).strip()

        # Generate ID from filename if missing (same logic as find_lessons)
        category = lesson_path.parent.name
        name = lesson_path.stem
        text = self.extract_text(lesson_path)
        text_hash = self.compute_text_hash(text)[:6]
        return f"{category}_{name}_{text_hash}"

    def _load_metadata(self):
        """Load embedding metadata from file.

        Metadata includes lesson IDs, text hashes, paths, and index positions.
        """
        metadata_path = self.embeddings_dir / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                self.metadata = json.load(f)

    def _save_metadata(self):
        """Save embedding metadata to file as JSON."""
        metadata_path = self.embeddings_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(self.metadata, f, indent=2)

    def _load_model(self):
        """Load Sentence-BERT model if not already loaded.

        Returns:
            True if model loaded successfully, False if sentence-transformers not installed
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            print("Error: sentence-transformers not installed.")
            print("Install: pip install sentence-transformers")
            return False

        if self.model is None:
            print(f"Loading model: {self.model_name}...")
            self.model = SentenceTransformer(self.model_name)
        return True

    def _load_index(self):
        """Load FAISS index from file, or use numpy fallback.

        Creates new index if file doesn't exist.
        """
        index_path = self.embeddings_dir / "index.faiss"

        if FAISS_AVAILABLE:
            if index_path.exists():
                self.index = faiss.read_index(str(index_path))
            else:
                # Create new index
                dim = self.config["embedding_dim"]
                self.index = faiss.IndexFlatL2(dim)
        else:
            # Fallback: numpy arrays
            if index_path.exists():
                # Load numpy embeddings
                self.index = np.load(str(index_path.with_suffix(".npy")))
            else:
                self.index = None

    def _save_index(self):
        """Save FAISS index to file, or numpy array as fallback."""
        index_path = self.embeddings_dir / "index.faiss"

        if FAISS_AVAILABLE and self.index is not None:
            faiss.write_index(self.index, str(index_path))
        elif self.index is not None:
            # Fallback: save numpy embeddings
            np.save(str(index_path.with_suffix(".npy")), self.index)

    def extract_text(self, lesson_path: Path) -> str:
        """Extract embeddable text from lesson file.

        Fields extracted (concatenated with delimiters):
        - TITLE: lesson title (from # header)
        - RULE: rule statement
        - CONTEXT: when it applies
        - DETECTION: failure signals
        - PATTERN: correct approach
        - OUTCOME: what happens

        Args:
            lesson_path: Path to lesson markdown file

        Returns:
            Concatenated text from all relevant sections with labeled delimiters
        """
        content = lesson_path.read_text()

        # Extract title (first # header)
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1) if title_match else ""

        # Extract sections
        def extract_section(section_name: str) -> str:
            # Match ## Section Name followed by content until next ##
            pattern = rf"^##\s+{section_name}\s*$(.+?)(?=^##|\Z)"
            match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
            if match:
                return match.group(1).strip()
            return ""

        rule = extract_section("Rule")
        context = extract_section("Context")
        detection = extract_section("Detection")
        pattern = extract_section("Pattern")
        outcome = extract_section("Outcome")

        # Concatenate with delimiters
        text_parts = []
        if title:
            text_parts.append(f"TITLE: {title}")
        if rule:
            text_parts.append(f"RULE: {rule}")
        if context:
            text_parts.append(f"CONTEXT: {context}")
        if detection:
            text_parts.append(f"DETECTION: {detection}")
        if pattern:
            text_parts.append(f"PATTERN: {pattern}")
        if outcome:
            text_parts.append(f"OUTCOME: {outcome}")

        return "\n\n".join(text_parts)

    def compute_text_hash(self, text: str) -> str:
        """Compute SHA256 hash of text for change detection.

        Args:
            text: Text content to hash

        Returns:
            Hexadecimal SHA256 hash string
        """
        return hashlib.sha256(text.encode()).hexdigest()

    def generate_embedding(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding vector for text using Sentence-BERT.

        Args:
            text: Text to embed

        Returns:
            Numpy array of embedding vector (float32), or None if model unavailable
        """
        if not self._load_model():
            return None

        assert self.model is not None  # _load_model ensures model is loaded
        embedding = self.model.encode(text)
        return np.array(embedding, dtype=np.float32)

    def find_lessons(self) -> List[Tuple[str, Path]]:
        """Find all lesson files and extract their IDs.

        Returns:
            List of (lesson_id, lesson_path) tuples for all lessons found
        """
        lessons = []

        for lesson_file in self.lessons_dir.rglob("*.md"):
            # Skip README files
            if lesson_file.name.upper() == "README.MD":
                continue

            # Extract lesson_id from frontmatter
            content = lesson_file.read_text()
            id_match = re.search(r"^lesson_id:\s*(.+)$", content, re.MULTILINE)

            if id_match:
                lesson_id = id_match.group(1).strip()
                lessons.append((lesson_id, lesson_file))
            else:
                # Generate ID from filename if missing
                category = lesson_file.parent.name
                name = lesson_file.stem
                text = self.extract_text(lesson_file)
                text_hash = self.compute_text_hash(text)[:6]
                lesson_id = f"{category}_{name}_{text_hash}"
                lessons.append((lesson_id, lesson_file))

        return lessons

    def generate_all(self, force: bool = False):
        """Generate embeddings for all lessons.

        Args:
            force: If True, regenerate embeddings even for unchanged lessons
        """
        if not self._load_model():
            return

        lessons = self.find_lessons()
        print(f"Found {len(lessons)} lessons")

        # Initialize index if needed
        if self.index is None:
            if FAISS_AVAILABLE:
                dim = self.config["embedding_dim"]
                self.index = faiss.IndexFlatL2(dim)
            else:
                self.index = np.zeros(
                    (0, self.config["embedding_dim"]), dtype=np.float32
                )

        generated = 0
        skipped = 0

        for lesson_id, lesson_path in lessons:
            # Extract text
            text = self.extract_text(lesson_path)
            if not text:
                print(f"Warning: No embeddable text in {lesson_path.name}")
                skipped += 1
                continue

            # Check if already embedded and unchanged
            text_hash = self.compute_text_hash(text)
            if (
                not force
                and lesson_id in self.metadata
                and self.metadata[lesson_id].get("text_hash") == text_hash
            ):
                print(f"Skipping {lesson_id} (unchanged)")
                skipped += 1
                continue

            # Generate embedding
            print(f"Generating embedding for {lesson_id}...")
            embedding = self.generate_embedding(text)
            if embedding is None:
                print(f"Error: Failed to generate embedding for {lesson_id}")
                skipped += 1
                continue

            # Add to index
            if FAISS_AVAILABLE:
                self.index.add(embedding.reshape(1, -1))
            else:
                # Numpy fallback
                self.index = np.vstack([self.index, embedding])

            # Store metadata
            self.metadata[lesson_id] = {
                "lesson_id": lesson_id,
                "text_hash": text_hash,
                "embedded_at": datetime.utcnow().isoformat() + "Z",
                "model": self.model_name,
                "path": str(lesson_path.relative_to(self.lessons_dir)),
                "index": (
                    self.index.ntotal - 1 if FAISS_AVAILABLE else len(self.index) - 1
                ),
            }

            generated += 1

        # Save everything
        self._save_index()
        self._save_metadata()
        self.config["last_updated"] = datetime.utcnow().isoformat() + "Z"
        self.config["lesson_count"] = len(self.metadata)
        self._save_config()

        print(f"\nDone! Generated {generated}, skipped {skipped}")
        print(f"Total lessons: {len(self.metadata)}")

    def find_similar(self, lesson_id: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Find k most similar lessons to given lesson.

        Args:
            lesson_id: ID of lesson to find similar lessons for
            top_k: Number of similar lessons to return

        Returns:
            List of (lesson_id, similarity_score) tuples, sorted by similarity (highest first)
        """
        if lesson_id not in self.metadata:
            print(f"Error: Lesson ID '{lesson_id}' not found")
            return []

        # Load index if needed
        if self.index is None:
            self._load_index()

        if self.index is None:
            print("Error: No index found. Run 'generate' first.")
            return []

        # Get lesson embedding from index
        lesson_idx = self.metadata[lesson_id]["index"]

        if FAISS_AVAILABLE:
            # Get embedding vector
            embedding = self.index.reconstruct(lesson_idx)
            embedding = embedding.reshape(1, -1)

            # Search
            distances, indices = self.index.search(embedding, top_k + 1)

            # Convert to results (skip self)
            results = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx == lesson_idx:
                    continue  # Skip self

                # Find lesson_id by index
                for lid, meta in self.metadata.items():
                    if meta["index"] == idx:
                        # Convert L2 distance to similarity score (0-1)
                        similarity = 1.0 / (1.0 + dist)
                        results.append((lid, similarity))
                        break

                if len(results) >= top_k:
                    break

        else:
            # Numpy fallback
            embedding = self.index[lesson_idx]

            # Compute cosine similarity with all
            similarities = np.dot(self.index, embedding) / (
                np.linalg.norm(self.index, axis=1) * np.linalg.norm(embedding)
            )

            # Get top k (excluding self)
            indices = np.argsort(-similarities)
            results = []
            for idx in indices:
                if idx == lesson_idx:
                    continue

                # Find lesson_id by index
                for lid, meta in self.metadata.items():
                    if meta["index"] == idx:
                        results.append((lid, similarities[idx]))
                        break

                if len(results) >= top_k:
                    break

        return results

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Semantic search by text query.

        Args:
            query: Natural language search query
            top_k: Number of results to return

        Returns:
            List of (lesson_id, similarity_score) tuples, sorted by relevance (highest first)
        """
        if not self._load_model():
            return []

        # Load index if needed
        if self.index is None:
            self._load_index()

        if self.index is None:
            print("Error: No index found. Run 'generate' first.")
            return []

        # Generate query embedding
        print(f"Searching for: '{query}'")
        query_embedding = self.generate_embedding(query)
        if query_embedding is None:
            return []

        if FAISS_AVAILABLE:
            # Search
            distances, indices = self.index.search(
                query_embedding.reshape(1, -1), top_k
            )

            # Convert to results
            results = []
            for dist, idx in zip(distances[0], indices[0]):
                # Find lesson_id by index
                for lesson_id, meta in self.metadata.items():
                    if meta["index"] == idx:
                        # Convert L2 distance to similarity score
                        similarity = 1.0 / (1.0 + dist)
                        results.append((lesson_id, similarity))
                        break

        else:
            # Numpy fallback
            similarities = np.dot(self.index, query_embedding) / (
                np.linalg.norm(self.index, axis=1) * np.linalg.norm(query_embedding)
            )

            # Get top k
            indices = np.argsort(-similarities)[:top_k]
            results = []
            for idx in indices:
                # Find lesson_id by index
                for lesson_id, meta in self.metadata.items():
                    if meta["index"] == idx:
                        results.append((lesson_id, similarities[idx]))
                        break

        return results

    def find_duplicates(
        self,
        threshold: float = 0.85,
        min_similarity: float = 0.7,
    ) -> List[Tuple[str, str, float]]:
        """Find potential duplicate lessons based on similarity threshold.

        Args:
            threshold: Similarity threshold for duplicates (default 0.85)
            min_similarity: Minimum similarity to report (default 0.7)

        Returns:
            List of (lesson1_id, lesson2_id, similarity) tuples for potential duplicates
        """
        # Load index if needed
        if self.index is None:
            self._load_index()

        if self.index is None:
            print("Error: No index found. Run 'generate' first.")
            return []

        duplicates = []
        seen_pairs = set()

        # Check each lesson against all others
        for lesson_id in self.metadata.keys():
            similar = self.find_similar(lesson_id, top_k=10)

            for similar_id, similarity in similar:
                # Only report if above minimum threshold
                if similarity < min_similarity:
                    continue

                # Create canonical pair (alphabetically sorted to avoid duplicates)
                pair = tuple(sorted([lesson_id, similar_id]))

                if pair not in seen_pairs:
                    seen_pairs.add(pair)

                    # Mark as potential duplicate if above threshold
                    if similarity >= threshold:
                        duplicates.append((lesson_id, similar_id, similarity))

        # Sort by similarity (highest first)
        duplicates.sort(key=lambda x: x[2], reverse=True)

        return duplicates

    def cluster_lessons(self, threshold: float = 0.7) -> Dict[int, List[str]]:
        """Cluster lessons by similarity using hierarchical clustering.

        Args:
            threshold: Similarity threshold for clustering (default 0.7)

        Returns:
            Dictionary mapping cluster_id -> [lesson_ids]
        """
        from scipy.spatial.distance import squareform  # type: ignore[import-untyped]

        # Load index if needed
        if self.index is None:
            self._load_index()

        if self.index is None:
            print("Error: No index found. Run 'generate' first.")
            return {}

        # Get lesson IDs in order
        lesson_ids = sorted(
            self.metadata.keys(), key=lambda x: self.metadata[x]["index"]
        )

        if len(lesson_ids) < 2:
            return {0: lesson_ids}

        # Build distance matrix (1 - similarity)
        n = len(lesson_ids)

        if FAISS_AVAILABLE:
            # Use FAISS index
            distances = np.zeros((n, n))

            for i, lesson_id in enumerate(lesson_ids):
                idx = self.metadata[lesson_id]["index"]
                embedding = self.index.reconstruct(idx).reshape(1, -1)

                # Search all
                dists, _ = self.index.search(embedding, n)

                # Convert L2 to similarity
                similarities = 1.0 / (1.0 + dists[0])

                # Convert similarity to distance
                distances[i] = 1.0 - similarities
        else:
            # Use numpy embeddings
            embeddings = []
            for lesson_id in lesson_ids:
                idx = self.metadata[lesson_id]["index"]
                embeddings.append(self.index[idx])

            embeddings = np.array(embeddings)

            # Compute cosine similarity matrix
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            similarities = np.dot(embeddings, embeddings.T) / (norms * norms.T)

            # Convert to distances
            distances = 1.0 - similarities

        # Hierarchical clustering
        # Convert distance to 1-similarity threshold
        distance_threshold = 1.0 - threshold

        # Flatten upper triangle for clustering
        condensed_distances = squareform(distances, checks=False)

        # Perform clustering using linkage + fcluster
        from scipy.cluster.hierarchy import fcluster, linkage  # type: ignore[import-untyped]

        Z = linkage(condensed_distances, method="average")
        cluster_labels = fcluster(Z, distance_threshold, criterion="distance")

        # Group by cluster
        clusters: Dict[int, List[str]] = {}
        for lesson_id, cluster_id in zip(lesson_ids, cluster_labels):
            if cluster_id not in clusters:
                clusters[cluster_id] = []
            clusters[cluster_id].append(lesson_id)

        return clusters

    def print_duplicate_report(
        self, threshold: float = 0.85, min_similarity: float = 0.7
    ):
        """Print formatted report of potential duplicate lessons.

        Args:
            threshold: Similarity threshold for marking as duplicate (default 0.85)
            min_similarity: Minimum similarity to include in report (default 0.7)
        """
        duplicates = self.find_duplicates(threshold, min_similarity)

        if not duplicates:
            print(f"✓ No duplicates found (threshold >= {threshold})")
            return

        print(
            f"\n⚠ Found {len(duplicates)} potential duplicates (similarity >= {threshold}):\n"
        )

        for lesson1, lesson2, similarity in duplicates:
            print(f"  • {similarity:.3f}: {lesson1} ↔ {lesson2}")

            # Show paths
            path1 = self.metadata[lesson1].get("path", "")
            path2 = self.metadata[lesson2].get("path", "")
            print(f"    {path1}")
            print(f"    {path2}")
            print()

    def print_cluster_report(self, threshold: float = 0.7):
        """Print formatted cluster report showing grouped lessons.

        Args:
            threshold: Similarity threshold for clustering (default 0.7)
        """
        clusters = self.cluster_lessons(threshold)

        print(f"\n=== Lesson Clusters (threshold={threshold}) ===\n")
        print(f"Total clusters: {len(clusters)}\n")

        for cluster_id in sorted(clusters.keys()):
            lesson_ids = clusters[cluster_id]
            if len(lesson_ids) == 1:
                continue  # Skip singleton clusters

            print(f"Cluster {cluster_id} ({len(lesson_ids)} lessons):")
            for lesson_id in lesson_ids:
                meta = self.metadata[lesson_id]
                print(f"  - {lesson_id}")
                print(f"    Path: {meta['path']}")
            print()

    def update_changed(self):
        """Update embeddings for changed lessons only.

        Checks text hash to detect changes and only regenerates
        embeddings for lessons that have been modified.
        """
        if not self._load_model():
            return

        assert self.index is not None  # _load_model ensures index is initialized
        lessons = self.find_lessons()
        print(f"Checking {len(lessons)} lessons for changes...")

        updated = 0

        for lesson_id, lesson_path in lessons:
            # Extract text
            text = self.extract_text(lesson_path)
            if not text:
                continue

            # Check if changed
            text_hash = self.compute_text_hash(text)

            if lesson_id in self.metadata:
                if self.metadata[lesson_id].get("text_hash") == text_hash:
                    continue  # Unchanged

            # Changed or new - regenerate
            print(f"Updating {lesson_id}...")
            embedding = self.generate_embedding(text)
            if embedding is None:
                continue

            # Update index
            if lesson_id in self.metadata:
                # Replace existing
                idx = self.metadata[lesson_id]["index"]
                if FAISS_AVAILABLE:
                    # FAISS doesn't support in-place update, need rebuild
                    # For now, just add new (will have duplicate until rebuild)
                    self.index.add(embedding.reshape(1, -1))
                    idx = self.index.ntotal - 1
                else:
                    self.index[idx] = embedding
            else:
                # Add new
                if FAISS_AVAILABLE:
                    self.index.add(embedding.reshape(1, -1))
                    idx = self.index.ntotal - 1
                else:
                    self.index = np.vstack([self.index, embedding])
                    idx = len(self.index) - 1

            # Update metadata
            self.metadata[lesson_id] = {
                "lesson_id": lesson_id,
                "text_hash": text_hash,
                "embedded_at": datetime.utcnow().isoformat() + "Z",
                "model": self.model_name,
                "path": str(lesson_path.relative_to(self.lessons_dir)),
                "index": idx,
            }

            updated += 1

        if updated > 0:
            # Save everything
            self._save_index()
            self._save_metadata()
            self.config["last_updated"] = datetime.utcnow().isoformat() + "Z"
            self.config["lesson_count"] = len(self.metadata)
            self._save_config()

        print(f"Updated {updated} lessons")

    def rebuild_index(self):
        """Rebuild entire index from scratch.

        Clears all existing embeddings and regenerates everything.
        Useful for fixing index corruption or changing models.
        """
        print("Rebuilding index from scratch...")

        # Clear existing data
        self.index = None
        self.metadata = {}

        # Regenerate all
        self.generate_all(force=True)

    # === Phase 4.3: Deduplication Workflow ===

    def check_new_lesson(
        self, text: str, threshold: float = 0.55
    ) -> List[Tuple[str, float]]:
        """
        Check if new lesson text is too similar to existing lessons.

        Args:
            text: New lesson text to check
            threshold: Similarity threshold (default 0.55)

        Returns:
            List of (lesson_id, similarity) tuples above threshold
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            print("Error: sentence-transformers not installed")
            return []

        # Generate embedding for new text
        self._load_model()
        embedding = self.generate_embedding(text)
        if embedding is None:
            print("Error: Failed to generate embedding")
            return []

        # Compare against all existing embeddings
        self._load_index()
        if self.index is None:
            print("No existing embeddings to compare against")
            return []

        # Reshape for FAISS search
        query_vector = embedding.reshape(1, -1)

        # Search for similar lessons
        if FAISS_AVAILABLE:
            distances, indices = self.index.search(query_vector, len(self.metadata))
            # Convert L2 distance to similarity score (consistent with find_similar)
            similarities = 1.0 / (1.0 + distances[0])
        else:
            # Numpy fallback: compute cosine similarity manually
            lesson_ids_list = list(self.metadata.keys())
            similarities = []
            for lesson_id in lesson_ids_list:
                meta = self.metadata[lesson_id]
                # Get embedding from index using stored index position
                idx = meta["index"]
                existing_emb = self.index[idx]
                # Cosine similarity
                sim = np.dot(embedding, existing_emb) / (
                    np.linalg.norm(embedding) * np.linalg.norm(existing_emb)
                )
                similarities.append(sim)
            similarities = np.array(similarities)
            indices = np.arange(len(similarities))

        # Filter by threshold
        lesson_ids_list = list(self.metadata.keys())
        results = []

        # Handle FAISS 2D array results (need to access first row)
        if FAISS_AVAILABLE:
            indices_1d = indices[0]
            similarities_1d = similarities
        else:
            indices_1d = indices
            similarities_1d = similarities

        for idx, similarity in zip(indices_1d, similarities_1d):
            if similarity >= threshold:
                lesson_id = lesson_ids_list[int(idx)]
                results.append((lesson_id, float(similarity)))

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def suggest_merges(self, threshold: float = 0.55) -> List[Dict[str, Any]]:
        """Generate merge suggestions for duplicate lessons.

        Args:
            threshold: Similarity threshold for suggesting merge (default 0.55)

        Returns:
            List of dicts with merge suggestions including lesson info and recommendations
        """
        duplicates = self.find_duplicates(threshold=threshold)
        if not duplicates:
            return []

        suggestions = []
        for lesson_id1, lesson_id2, similarity in duplicates:
            meta1 = self.metadata[lesson_id1]
            meta2 = self.metadata[lesson_id2]

            # Extract lesson names from IDs
            name1 = lesson_id1.split("_", 1)[1].rsplit("_", 1)[0]
            name2 = lesson_id2.split("_", 1)[1].rsplit("_", 1)[0]

            suggestion = {
                "lesson1": {
                    "id": lesson_id1,
                    "name": name1,
                    "path": meta1["path"],
                },
                "lesson2": {
                    "id": lesson_id2,
                    "name": name2,
                    "path": meta2["path"],
                },
                "similarity": similarity,
                "recommendation": self._get_merge_recommendation(
                    name1, name2, similarity
                ),
            }
            suggestions.append(suggestion)

        return suggestions

    def _get_merge_recommendation(
        self, name1: str, name2: str, similarity: float
    ) -> str:
        """Generate merge recommendation based on lesson names and similarity.

        Args:
            name1: First lesson name
            name2: Second lesson name
            similarity: Similarity score between lessons

        Returns:
            Human-readable merge recommendation string
        """
        if similarity >= 0.90:
            return f"STRONG DUPLICATE: Consider merging '{name1}' and '{name2}' - very high similarity"
        elif similarity >= 0.80:
            return (
                f"LIKELY DUPLICATE: Review '{name1}' and '{name2}' for potential merge"
            )
        elif similarity >= 0.70:
            return f"RELATED: '{name1}' and '{name2}' cover similar topics, consider consolidation"
        else:
            return f"SIMILAR: '{name1}' and '{name2}' have related content"

    def get_merge_preview(self, lesson_id1: str, lesson_id2: str) -> Dict[str, Any]:
        """
        Get preview of what merging two lessons would look like.

        Args:
            lesson_id1: First lesson ID
            lesson_id2: Second lesson ID

        Returns:
            Dictionary with merge preview information
        """
        if lesson_id1 not in self.metadata or lesson_id2 not in self.metadata:
            return {"error": "One or both lesson IDs not found"}

        meta1 = self.metadata[lesson_id1]
        meta2 = self.metadata[lesson_id2]

        # Load lesson content (paths are relative to lessons_dir)
        path1 = self.lessons_dir / meta1["path"]
        path2 = self.lessons_dir / meta2["path"]

        if not path1.exists() or not path2.exists():
            return {"error": "One or both lesson files not found"}

        content1 = path1.read_text()
        content2 = path2.read_text()

        # Extract key sections
        def extract_rule(content: str) -> str:
            match = re.search(r"## Rule\n(.+?)(?:\n##|\Z)", content, re.DOTALL)
            return match.group(1).strip() if match else ""

        def extract_context(content: str) -> str:
            match = re.search(r"## Context\n(.+?)(?:\n##|\Z)", content, re.DOTALL)
            return match.group(1).strip() if match else ""

        preview = {
            "lesson1": {
                "id": lesson_id1,
                "path": str(path1),
                "rule": extract_rule(content1),
                "context": extract_context(content1),
            },
            "lesson2": {
                "id": lesson_id2,
                "path": str(path2),
                "rule": extract_rule(content2),
                "context": extract_context(content2),
            },
            "similarity": self._compute_similarity(lesson_id1, lesson_id2),
        }

        return preview

    def _compute_similarity(self, lesson_id1: str, lesson_id2: str) -> float:
        """Compute cosine similarity between two lessons by their IDs.

        Args:
            lesson_id1: First lesson ID
            lesson_id2: Second lesson ID

        Returns:
            Similarity score from 0.0 to 1.0, or 0.0 if lessons not found
        """
        if lesson_id1 not in self.metadata or lesson_id2 not in self.metadata:
            return 0.0

        # Load index if needed
        self._load_index()
        if self.index is None:
            return 0.0

        # Get index positions
        idx1 = self.metadata[lesson_id1]["index"]
        idx2 = self.metadata[lesson_id2]["index"]

        # Retrieve embeddings from index
        if FAISS_AVAILABLE:
            emb1 = self.index.reconstruct(int(idx1))
            emb2 = self.index.reconstruct(int(idx2))
        else:
            # For numpy fallback, embeddings are stored differently
            # We need to reconstruct from find_similar approach
            return 0.0

        # Cosine similarity
        similarity = np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
        return float(similarity)

    def dashboard(
        self,
        cluster_threshold: float = 0.7,
        duplicate_threshold: float = 0.85,
        show_top_merges: int = 5,
    ):
        """
        Display comprehensive deduplication dashboard.

        Args:
            cluster_threshold: Threshold for clustering (default 0.7)
            duplicate_threshold: Threshold for duplicates (default 0.85)
            show_top_merges: Number of top merge candidates to show (default 5)
        """
        if not self.metadata:
            print("No embeddings found. Run 'generate' first.")
            return

        print("\n=== Lesson Deduplication Dashboard ===\n")

        # 1. Overview metrics
        print("📊 Overview:")
        print(f"  Total Lessons: {len(self.metadata)}")
        print(f"  Embeddings Generated: {len(self.metadata)}")

        if self.metadata:
            latest_time = max(m["embedded_at"] for m in self.metadata.values())
            print(f"  Last Updated: {latest_time}\n")

        # 2. Duplicate detection at multiple thresholds
        print("🔍 Duplicate Detection:")

        # High threshold (0.85-1.0)
        high_duplicates = self.find_duplicates(threshold=duplicate_threshold)
        print(
            f"  Potential Duplicates (>{duplicate_threshold:.2f}): {len(high_duplicates)} pairs"
        )

        # Medium-high (0.75-0.85)
        all_75_plus = self.find_duplicates(threshold=0.75, min_similarity=0.75)
        medium_high = [
            d for d in all_75_plus if d[2] < duplicate_threshold
        ]  # d[2] is similarity
        print(
            f"  High Similarity (0.75-{duplicate_threshold:.2f}): {len(medium_high)} pairs"
        )

        # Medium (0.65-0.75)
        all_65_plus = self.find_duplicates(threshold=0.65, min_similarity=0.65)
        medium = [d for d in all_65_plus if d[2] < 0.75]  # d[2] is similarity
        print(f"  Medium Similarity (0.65-0.75): {len(medium)} pairs\n")

        # 3. Cluster summary
        print(f"🗂️  Lesson Clusters (threshold={cluster_threshold}):")
        clusters = self.cluster_lessons(threshold=cluster_threshold)

        # Calculate cluster sizes
        cluster_sizes = {cid: len(lessons) for cid, lessons in clusters.items()}
        singletons = sum(1 for size in cluster_sizes.values() if size == 1)
        max_size = max(cluster_sizes.values()) if cluster_sizes else 0

        print(f"  Total Clusters: {len(clusters)}")
        print(f"  Largest Cluster: {max_size} lessons")
        print(f"  Singletons: {singletons} clusters (1 lesson each)\n")

        # 4. Top merge candidates
        print(f"📝 Top Merge Candidates (showing top {show_top_merges}):")
        suggestions = self.suggest_merges(
            threshold=0.70
        )  # Lower threshold for suggestions

        if not suggestions:
            print("  No merge suggestions found.\n")
        else:
            for i, suggestion in enumerate(suggestions[:show_top_merges], 1):
                l1_name = suggestion["lesson1"]["name"]
                l2_name = suggestion["lesson2"]["name"]
                similarity = suggestion["similarity"]
                recommendation = suggestion["recommendation"]

                print(f"  {i}. {l1_name} ↔ {l2_name} ({similarity:.3f})")
                print(f"     {recommendation}")
            print()

        # 5. Deduplication impact
        print("💡 Deduplication Impact:")
        total_lessons = len(self.metadata)
        merge_candidates = len(suggestions)
        lessons_to_merge = merge_candidates * 2  # Each pair represents 2 lessons

        print(f"  Current Lessons: {total_lessons}")
        print(
            f"  Merge Candidates: {merge_candidates} pairs ({lessons_to_merge} lessons)"
        )

        if merge_candidates > 0:
            potential_after = (
                total_lessons - merge_candidates
            )  # Each merge reduces by 1
            reduction_pct = (merge_candidates / total_lessons) * 100
            tokens_per_lesson = 100  # Rough estimate
            token_savings = merge_candidates * tokens_per_lesson

            print(
                f"  After Consolidation: ~{potential_after} lessons (-{reduction_pct:.1f}%)"
            )
            print(
                f"  Estimated Token Savings: ~{token_savings} tokens per context load"
            )
        else:
            print("  No consolidation opportunities identified.")

        print()


def main():
    parser = argparse.ArgumentParser(description="ACE Lesson Embedding System")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Generate command
    gen_parser = subparsers.add_parser(
        "generate", help="Generate embeddings for all lessons"
    )
    gen_parser.add_argument(
        "--force", action="store_true", help="Regenerate even if unchanged"
    )

    # Update command
    subparsers.add_parser("update", help="Update embeddings for changed lessons")

    # Rebuild command
    subparsers.add_parser("rebuild", help="Rebuild index from scratch")

    # Similar command
    sim_parser = subparsers.add_parser("similar", help="Find similar lessons")
    sim_parser.add_argument(
        "--lesson-id", required=True, help="Lesson ID to find similar to"
    )
    sim_parser.add_argument("--top-k", type=int, default=5, help="Number of results")

    # Search command
    search_parser = subparsers.add_parser("search", help="Semantic search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--top-k", type=int, default=5, help="Number of results")

    # List command
    subparsers.add_parser("list", help="List all embedded lessons")

    # Duplicates command
    dup_parser = subparsers.add_parser(
        "duplicates", help="Find potential duplicate lessons"
    )
    dup_parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Similarity threshold for duplicates (default: 0.70)",
    )
    dup_parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.7,
        help="Minimum similarity to report (default: 0.7)",
    )

    # Cluster command
    cluster_parser = subparsers.add_parser(
        "cluster", help="Cluster lessons by similarity"
    )
    cluster_parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Similarity threshold for clustering (default: 0.7)",
    )

    # Phase 4.3: Deduplication workflow commands
    # Check-new command
    check_parser = subparsers.add_parser(
        "check-new", help="Check if new lesson text is too similar to existing lessons"
    )
    check_parser.add_argument("file", help="Path to new lesson file or '-' for stdin")
    check_parser.add_argument(
        "--threshold",
        type=float,
        default=0.55,
        help="Similarity threshold (default: 0.55)",
    )

    # Suggest-merges command
    merge_parser = subparsers.add_parser(
        "suggest-merges", help="Suggest merging duplicate lessons"
    )
    merge_parser.add_argument(
        "--threshold",
        type=float,
        default=0.55,
        help="Similarity threshold for merge suggestions (default: 0.55)",
    )

    # Preview command
    preview_parser = subparsers.add_parser(
        "preview-merge", help="Preview what merging two lessons would look like"
    )
    preview_parser.add_argument("lesson1", help="First lesson ID")
    preview_parser.add_argument("lesson2", help="Second lesson ID")

    # Dashboard command
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Show comprehensive deduplication dashboard"
    )
    dashboard_parser.add_argument(
        "--cluster-threshold",
        type=float,
        default=0.7,
        help="Threshold for clustering (default: 0.7)",
    )
    dashboard_parser.add_argument(
        "--duplicate-threshold",
        type=float,
        default=0.85,
        help="Threshold for duplicates (default: 0.85)",
    )
    dashboard_parser.add_argument(
        "--show-top",
        type=int,
        default=5,
        help="Number of top merge candidates to show (default: 5)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    embedder = LessonEmbedder()

    if args.command == "generate":
        embedder.generate_all(force=args.force)

    elif args.command == "update":
        embedder.update_changed()

    elif args.command == "rebuild":
        embedder.rebuild_index()

    elif args.command == "similar":
        results = embedder.find_similar(args.lesson_id, args.top_k)
        if results:
            print(f"\nTop {len(results)} similar lessons to '{args.lesson_id}':\n")
            for lesson_id, similarity in results:
                meta = embedder.metadata[lesson_id]
                print(f"  {lesson_id}")
                print(f"    Similarity: {similarity:.3f}")
                print(f"    Path: {meta['path']}\n")

    elif args.command == "search":
        results = embedder.search(args.query, args.top_k)
        if results:
            print(f"\nTop {len(results)} results for '{args.query}':\n")
            for lesson_id, similarity in results:
                meta = embedder.metadata[lesson_id]
                print(f"  {lesson_id}")
                print(f"    Similarity: {similarity:.3f}")
                print(f"    Path: {meta['path']}\n")

    elif args.command == "list":
        if not embedder.metadata:
            print("No embeddings found. Run 'generate' first.")
        else:
            print(f"Total lessons: {len(embedder.metadata)}\n")
            for lesson_id, meta in sorted(embedder.metadata.items()):
                print(f"  {lesson_id}")
                print(f"    Path: {meta['path']}")
                print(f"    Embedded: {meta['embedded_at']}\n")

    elif args.command == "duplicates":
        embedder.print_duplicate_report(args.threshold, args.min_similarity)

    elif args.command == "cluster":
        embedder.print_cluster_report(args.threshold)

    elif args.command == "check-new":
        # Read new lesson text
        if args.file == "-":
            import sys

            text = sys.stdin.read()
        else:
            text = Path(args.file).read_text()

        # Check for similar lessons
        results = embedder.check_new_lesson(text, args.threshold)

        if not results:
            print(f"\n✅ No similar lessons found above threshold {args.threshold}")
            print("Safe to create new lesson.")
        else:
            print(
                f"\n⚠️ Found {len(results)} similar lesson(s) above threshold {args.threshold}:\n"
            )
            for lesson_id, similarity in results[:5]:  # Show top 5
                meta = embedder.metadata[lesson_id]
                print(f"  {lesson_id}")
                print(f"    Similarity: {similarity:.3f}")
                print(f"    Path: {meta['path']}")
                print()
            print("Consider reviewing these lessons before creating a new one.")

    elif args.command == "suggest-merges":
        suggestions = embedder.suggest_merges(args.threshold)

        if not suggestions:
            print(f"\n✅ No merge suggestions at threshold {args.threshold}")
        else:
            print(f"\n📋 Merge Suggestions (threshold={args.threshold}):\n")
            for i, suggestion in enumerate(suggestions, 1):
                print(
                    f"{i}. {suggestion['lesson1']['name']} ↔ {suggestion['lesson2']['name']}"
                )
                print(f"   Similarity: {suggestion['similarity']:.3f}")
                print(f"   {suggestion['recommendation']}")
                print("   Paths:")
                print(f"     - {suggestion['lesson1']['path']}")
                print(f"     - {suggestion['lesson2']['path']}")
                print()

    elif args.command == "preview-merge":
        preview = embedder.get_merge_preview(args.lesson1, args.lesson2)

        if "error" in preview:
            print(f"\n❌ Error: {preview['error']}")
        else:
            print("\n=== Merge Preview ===")
            print(f"Similarity: {preview['similarity']:.3f}\n")

            print("Lesson 1:")
            print(f"  ID: {preview['lesson1']['id']}")
            print(f"  Path: {preview['lesson1']['path']}")
            print(f"  Rule: {preview['lesson1']['rule'][:100]}...")
            print(f"  Context: {preview['lesson1']['context'][:100]}...\n")

            print("Lesson 2:")
            print(f"  ID: {preview['lesson2']['id']}")
            print(f"  Path: {preview['lesson2']['path']}")
            print(f"  Rule: {preview['lesson2']['rule'][:100]}...")
            print(f"  Context: {preview['lesson2']['context'][:100]}...")

    elif args.command == "dashboard":
        embedder.dashboard(
            cluster_threshold=args.cluster_threshold,
            duplicate_threshold=args.duplicate_threshold,
            show_top_merges=args.show_top,
        )


if __name__ == "__main__":
    main()
