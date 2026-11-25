"""File watcher for automatic index updates."""

import logging
import os
import time
from pathlib import Path
from threading import Lock, Timer
from datetime import datetime

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .indexer import Indexer

logger = logging.getLogger(__name__)


class IndexEventHandler(FileSystemEventHandler):
    """Handle file system events for index updates."""

    def __init__(
        self,
        indexer: Indexer,
        pattern: str = "**/*.*",
        ignore_patterns: list[str] | None = None,
    ):
        """Initialize the event handler.

        Args:
            indexer: The indexer to update
            pattern: Glob pattern for files to index
            ignore_patterns: List of glob patterns to ignore
        """
        self.indexer = indexer
        self.pattern = pattern
        self.ignore_patterns = ignore_patterns or [".git", "__pycache__", "*.pyc"]
        self._pending_updates: set[Path] = set()
        self._last_update = time.time()
        self._update_delay = 1.0  # seconds
        self._lock = Lock()
        self._timers: dict[Path, Timer] = {}

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        src = str(event.src_path)
        if not event.is_directory and self._should_process(src):
            self._queue_update(Path(src))

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        src = str(event.src_path)
        if not event.is_directory and self._should_process(src):
            self._queue_update(Path(src))

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Handle file deletion events.

        Moving a directory out of the watched tree produces only a
        ``DirDeletedEvent`` (no per-file events), so a deleted directory
        removes every indexed source beneath it.
        """
        src = str(event.src_path)
        if event.is_directory:
            # No ignore-pattern filter here: neither the initial index nor the
            # file-event filters exclude by ancestor, so any directory (even
            # node_modules or __pycache__) may hold indexed sources.
            if not self._is_persist_path(Path(src)):
                self._delete_directory(Path(src).resolve())
        elif self._should_process(src):
            self._queue_deletion(Path(src))

    def _delete_directory(self, directory: Path) -> None:
        """Remove every indexed source under ``directory`` from the index."""
        prefix = os.path.join(str(directory), "")
        with self._lock:
            for pending in [p for p in self._timers if str(p).startswith(prefix)]:
                self._timers.pop(pending).cancel()
            self._remove_directory_sources(directory, prefix)

    def _remove_directory_sources(self, directory: Path, prefix: str) -> None:
        """Scan and remove sources while serialized with timer callbacks."""
        sources: set[str] = set()
        page_size = 1000
        offset = 0
        try:
            while True:
                page = self.indexer.collection.get(
                    include=["metadatas"], limit=page_size, offset=offset
                )
                ids = page["ids"] or []
                for meta in page["metadatas"] or []:
                    source = str((meta or {}).get("source", ""))
                    if source.startswith(prefix):
                        sources.add(source)
                if len(ids) < page_size:
                    break
                offset += len(ids)
            if not sources:
                return
            logger.info(f"Removing {len(sources)} file(s) under deleted directory {directory}")
            for source in sorted(sources):
                self.indexer.delete_documents({"source": source})
            self.indexer.cache.clear()
        except Exception as e:
            logger.error(f"Error removing directory {directory} from index: {e}", exc_info=True)

    def _should_process(self, path: str) -> bool:
        """Check if a file should be processed based on pattern and ignore patterns."""
        path_obj = Path(path)
        if self._is_persist_path(path_obj):
            return False
        return path_obj.match(self.pattern) and not any(
            path_obj.match(pattern) for pattern in self.ignore_patterns
        )

    def _queue_update(self, path: Path) -> None:
        """Queue a file for update with debouncing.

        Multiple rapid events for the same file are coalesced into a single update.
        """
        resolved_path = path.resolve()

        with self._lock:
            if self._should_skip_file(path, set()):
                return

            # Cancel any existing timer for this path.
            if resolved_path in self._timers:
                self._timers[resolved_path].cancel()

            # Schedule update after delay (debouncing)
            timer = Timer(self._update_delay, lambda: self._process_update(resolved_path, timer))
            self._timers[resolved_path] = timer
            timer.start()
            logger.debug(f"Queued update for {path} (delay: {self._update_delay}s)")

    def _queue_deletion(self, path: Path) -> None:
        """Queue a file for deletion from the index with debouncing."""
        resolved_path = path.resolve()

        with self._lock:
            # Cancel any pending update timer for this path
            if resolved_path in self._timers:
                self._timers[resolved_path].cancel()

            # Schedule deletion after delay (debouncing)
            timer = Timer(self._update_delay, lambda: self._process_deletion(resolved_path, timer))
            self._timers[resolved_path] = timer
            timer.start()
            logger.debug(f"Queued deletion for {path} (delay: {self._update_delay}s)")

    def _process_deletion(self, path: Path, timer: Timer | None = None) -> None:
        """Process a deletion only if this exact timer is still current."""
        canonical_path = str(path)
        # Keep validation and mutation atomic with moves and new events.
        with self._lock:
            if timer is not None and self._timers.get(path) is not timer:
                logger.debug(f"Skipping superseded deletion for {path}")
                return
            self._timers.pop(path, None)
            logger.info(f"Removing deleted file from index: {canonical_path}")
            try:
                self.indexer.delete_documents({"source": canonical_path})
                self.indexer.cache.clear()
                logger.debug(f"Successfully removed {canonical_path} from index")
            except Exception as e:
                logger.error(f"Error removing {canonical_path} from index: {e}", exc_info=True)

    def _process_update(self, path: Path, timer: Timer | None = None) -> None:
        """Process an update only if this exact timer is still current."""
        with self._lock:
            if timer is not None and self._timers.get(path) is not timer:
                logger.debug(f"Skipping superseded update for {path}")
                return
            self._timers.pop(path, None)
            self._update_file(path)

    def _update_file(self, path: Path) -> None:
        """Update the index while serialized with moves and deletions."""
        logger.debug(f"Processing update for {path}")

        try:
            if not path.exists():
                logger.debug(f"File no longer exists, skipping: {path}")
                return

            # Read file content to ensure it's readable
            content = path.read_text(encoding="utf-8")
            canonical_path = str(path)

            # Delete old versions
            logger.debug(f"Deleting old versions for {canonical_path}")
            self.indexer.delete_documents({"source": canonical_path})

            # Index new content
            logger.debug(f"Indexing new content for {canonical_path}")
            n_indexed = self.indexer.index_file(path)
            if n_indexed == 0:
                logger.warning(f"No documents indexed for {path}")
                return

            # Clear search cache to ensure fresh results
            self.indexer.cache.clear()

            # Verify the update
            logger.debug(f"Verifying update for {canonical_path}")
            results, _, _ = self.indexer.search(content[:100], n_results=1)
            if not results:
                logger.warning(f"No results found after indexing {path}")
            elif canonical_path not in str(results[0].metadata.get("source", "")):
                logger.warning(f"Found results but source doesn't match for {path}")
            else:
                logger.debug(f"Successfully updated {path}")

        except FileNotFoundError:
            logger.debug(f"File no longer exists (likely moved/deleted): {path}")
        except Exception as e:
            logger.error(f"Error updating {path}: {e}", exc_info=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Serialize file moves with callbacks that may already have fired."""
        with self._lock:
            self._process_move(event)

    def _process_move(self, event: FileSystemEvent) -> None:
        """Replace source and destination while holding the mutation lock."""
        if not event.is_directory:
            src_str = str(event.src_path)
            dest_str = str(event.dest_path)
            logger.info(f"File moved: {src_str} -> {dest_str}")
            src_path = Path(src_str).resolve()
            dest_path = Path(dest_str).resolve()

            # Removing the registration also supersedes an already-fired timer.
            pending_src = self._timers.pop(src_path, None)
            if pending_src is not None:
                pending_src.cancel()

            # Remove old file from index if it was being tracked
            if self._should_process(src_str):
                logger.debug(f"Removing old path from index: {src_path}")
                try:
                    self.indexer.delete_documents({"source": str(src_path)})
                    # The search cache may still hold results for the old path,
                    # and nothing below clears it when dest is not indexed.
                    self.indexer.cache.clear()
                    logger.debug(f"Deleted old documents for: {src_path}")
                except Exception as e:
                    logger.warning(f"Error removing old path {src_path} from index: {e}")

            # Index the file at its new location if it matches our patterns
            if self._should_process(dest_str):
                logger.info(f"Indexing moved file at new location: {dest_path}")

                # A pending or already-fired timer belongs to the old dest.
                # Callbacks check their own identity, not a path tombstone,
                # so later events cannot accidentally re-enable an old timer.
                pending = self._timers.pop(dest_path, None)
                if pending is not None:
                    pending.cancel()

                # Renaming over an existing file (an atomic save) replaces its
                # content, so drop the old chunks before indexing the new ones.
                try:
                    self.indexer.delete_documents({"source": str(dest_path)})
                    # Invalidate even if the replacement cannot be read/indexed.
                    self.indexer.cache.clear()
                except Exception as e:
                    logger.warning(f"Error removing replaced content for {dest_path}: {e}")

                # Wait for the file to be fully moved and readable
                max_attempts = 3
                for attempt in range(max_attempts):
                    try:
                        # Try to read the file to ensure it's ready
                        content = dest_path.read_text(encoding="utf-8")
                        # Index the file
                        self.indexer.index_file(dest_path)

                        # Clear search cache to ensure fresh results for verification
                        self.indexer.cache.clear()

                        # Verify the update with content-based search
                        results, _, _ = self.indexer.search(
                            content[:50]
                        )  # Search by content prefix
                        if results and any(
                            str(dest_path) == doc.metadata.get("source") for doc in results
                        ):
                            logger.info(f"Successfully verified moved file: {dest_path}")
                            break
                        elif attempt < max_attempts - 1:
                            logger.warning(
                                f"Verification failed, retrying... ({attempt + 1}/{max_attempts})"
                            )
                            time.sleep(0.5)  # Wait before retry
                        else:
                            logger.error(
                                f"Failed to verify moved file after {max_attempts} attempts: {dest_path}"
                            )
                    except Exception as e:
                        if attempt < max_attempts - 1:
                            logger.warning(
                                f"Error processing moved file (attempt {attempt + 1}): {e}"
                            )
                            time.sleep(0.5)  # Wait before retry
                        else:
                            logger.error(
                                f"Failed to process moved file after {max_attempts} attempts: {e}"
                            )

    def _should_skip_file(self, path: Path, processed_paths: set[str]) -> bool:
        """Check if a file should be skipped during processing."""
        canonical_path = str(path.resolve())

        # Skip if already processed
        if canonical_path in processed_paths:
            logger.debug(f"Skipping already processed file: {path}")
            return True

        # Skip if not a file
        if not path.is_file():
            logger.debug(f"Skipping non-file: {path}")
            return True

        # Skip persist-dir internals (lock file, chroma sqlite, segment dirs)
        if self._is_persist_path(path):
            logger.debug(f"Skipping persist-dir artifact: {path}")
            return True

        # Skip binary and system files
        if path.suffix in {".sqlite3", ".db", ".bin", ".pyc", ".lock", ".git"}:
            logger.debug(f"Skipping binary/system file: {path}")
            return True

        # Skip if doesn't match pattern
        if not path.match(self.pattern):
            logger.debug(f"Skipping file not matching pattern {self.pattern}: {path}")
            return True

        # Skip if matches ignore patterns
        if any(path.match(pattern) for pattern in self.ignore_patterns):
            logger.debug(f"Skipping file matching ignore pattern: {path}")
            return True

        logger.debug(f"File will be processed: {path}")
        return False

    def _is_persist_path(self, path: Path) -> bool:
        """Skip the indexer's persist dir — those files are the index, not corpus."""
        is_persist = getattr(self.indexer, "_is_persist_artifact", None)
        if callable(is_persist):
            return bool(is_persist(path))
        persist = getattr(self.indexer, "persist_directory", None)
        if persist is None:
            return False
        try:
            persist_resolved = Path(persist).resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved == persist_resolved or persist_resolved in resolved.parents

    def _update_index_with_retries(self, path: Path, content: str, max_attempts: int = 5) -> bool:
        """Update index for a file with retries."""
        canonical_path = str(path.resolve())

        # Delete old versions
        try:
            self.indexer.delete_documents({"source": canonical_path})
            logger.debug(f"Cleared old versions for: {canonical_path}")
        except Exception as e:
            logger.warning(f"Error clearing old versions for {canonical_path}: {e}")

        # Try indexing with verification
        for attempt in range(max_attempts):
            try:
                # Exponential backoff
                if attempt > 0:
                    wait_time = 0.5 * (2**attempt)
                    logger.debug(f"Waiting {wait_time}s before retry {attempt + 1}")
                    time.sleep(wait_time)

                logger.info(f"Indexing attempt {attempt + 1} for {path}")

                # Index the file
                n_indexed = self.indexer.index_file(path)
                if n_indexed == 0:
                    logger.warning(f"No documents indexed for {path}")
                    continue

                # Verify with multiple search attempts
                for verify_attempt in range(3):
                    if verify_attempt > 0:
                        time.sleep(0.2)
                    if self.indexer.verify_document(path, content=content):
                        logger.info(f"Successfully verified index update for {path}")
                        return True
                    logger.debug(f"Verification attempt {verify_attempt + 1} failed")

                if attempt < max_attempts - 1:
                    logger.warning(
                        f"Verification failed, retrying... ({attempt + 1}/{max_attempts})"
                    )

            except Exception as e:
                logger.error(f"Error during indexing attempt {attempt + 1}: {e}", exc_info=True)
                if attempt == max_attempts - 1:
                    raise

        logger.error(f"Failed to verify index update after {max_attempts} attempts for {path}")
        return False

    def _process_single_update(self, path: Path, processed_paths: set[str]) -> None:
        """Process a single file update.

        Args:
            path: Path to the file to process
            processed_paths: Set of already processed canonical paths
        """
        if self._should_skip_file(path, processed_paths):
            return

        max_attempts = 3
        base_delay = 0.2

        for attempt in range(max_attempts):
            try:
                # Exponential backoff for retries
                wait_time = base_delay * (2**attempt)
                time.sleep(wait_time)

                if not path.exists():
                    logger.warning(f"File no longer exists: {path}")
                    return

                # Read current content for verification
                try:
                    current_content = path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Failed to read file {path}: {e}")
                    if attempt < max_attempts - 1:
                        continue
                    else:
                        raise

                # Clear old versions before updating
                canonical_path = str(path.resolve())
                self.indexer.delete_documents({"source": canonical_path})
                logger.debug(f"Cleared old versions of {canonical_path}")

                # Update index
                if self._update_index_with_retries(path, current_content):
                    processed_paths.add(canonical_path)
                    logger.info(f"Successfully processed update for {path}")
                    return

            except Exception as e:
                logger.error(
                    f"Error processing update for {path} (attempt {attempt + 1}): {e}",
                    exc_info=True,
                )
                if attempt == max_attempts - 1:
                    raise

    def _process_updates(self) -> None:
        """Process all pending updates."""
        if not self._pending_updates:
            logger.debug("No pending updates to process")
            return

        # Get all pending updates that still exist
        existing_updates = [p for p in self._pending_updates if p.exists()]
        if not existing_updates:
            logger.debug("No existing files to process")
            return

        logger.info(f"Processing {len(existing_updates)} updates")

        # Sort updates by modification time to get latest versions
        updates = sorted(
            existing_updates,
            key=lambda p: datetime.fromtimestamp(p.stat().st_mtime),
            reverse=True,
        )
        logger.debug(f"Sorted updates: {[str(p) for p in updates]}")

        # Process only the latest version of each file
        processed_paths: set[str] = set()
        for path in updates:
            self._process_single_update(path, processed_paths)

        self._pending_updates.clear()
        self._last_update = time.time()
        logger.info("Finished processing updates")

    def cancel_pending_updates(self) -> None:
        """Cancel all pending update timers."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class FileWatcher:
    """Watch files and update the index automatically."""

    def __init__(
        self,
        indexer: Indexer,
        paths: list[str],
        pattern: str = "**/*.*",
        ignore_patterns: list[str] | None = None,
        update_delay: float = 1.0,
    ):
        """Initialize the file watcher.

        Args:
            indexer: The indexer to update
            paths: List of paths to watch
            pattern: Glob pattern for files to index
            ignore_patterns: List of glob patterns to ignore
            update_delay: Delay between updates (0 for immediate updates in tests)
        """
        self.indexer = indexer
        self.paths = [Path(p) for p in paths]
        self.event_handler = IndexEventHandler(indexer, pattern, ignore_patterns)
        # For tests, use minimal delays
        if update_delay == 0:
            self.event_handler._update_delay = 0.1
            self.startup_delay = 0.5
        else:
            self.event_handler._update_delay = update_delay
            self.startup_delay = 2.0
        self.observer = Observer()

    def start(self) -> None:
        """Start watching for file changes."""
        # Reset collection and prepare paths
        self.indexer.reset_collection()

        for path in self.paths:
            path.mkdir(parents=True, exist_ok=True)
            self.indexer.index_directory(path, self.event_handler.pattern)
            self.observer.schedule(self.event_handler, str(path), recursive=True)

        # Start observer and wait for it to be ready
        self.observer.start()
        time.sleep(self.startup_delay)

    def stop(self) -> None:
        """Stop watching for file changes."""
        # Cancel any pending update timers before stopping
        self.event_handler.cancel_pending_updates()
        self.observer.stop()
        self.observer.join()
        logger.info("Stopped file watcher")

    def __enter__(self) -> "FileWatcher":
        """Start watching when used as context manager."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop watching when exiting context manager."""
        self.stop()
