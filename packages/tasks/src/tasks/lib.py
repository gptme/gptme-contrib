"""Core business logic for the tasks package.

This module contains the core business logic that sits between
the CLI layer (cli.py) and utility functions (utils.py).

Import chain: cli.py -> lib.py -> utils.py
"""

from tasks.utils import (
    # Data classes
    DirectoryConfig,
    SubtaskCount,
    TaskInfo,
    # Constants
    CONFIGS,
    PRIORITY_RANK,
    STATE_STYLES,
    STATE_EMOJIS,
    # Core utilities
    find_repo_root,
    format_time_ago,
    count_subtasks,
    validate_task_file,
    load_task,
    load_tasks,
    task_to_dict,
    is_task_ready,
    resolve_tasks,
    StateChecker,
    # Tracking and state
    parse_tracking_ref,
    fetch_github_issue_state,
    fetch_linear_issue_state,
    update_task_state,
    # Cache
    get_cache_path,
    load_cache,
    save_cache,
    # URLs
    extract_external_urls,
    fetch_url_state,
)

# Re-export everything for consumers
__all__ = [
    # Data classes
    "DirectoryConfig",
    "SubtaskCount",
    "TaskInfo",
    # Constants
    "CONFIGS",
    "PRIORITY_RANK",
    "STATE_STYLES",
    "STATE_EMOJIS",
    # Core utilities
    "find_repo_root",
    "format_time_ago",
    "count_subtasks",
    "validate_task_file",
    "load_task",
    "load_tasks",
    "task_to_dict",
    "is_task_ready",
    "resolve_tasks",
    "StateChecker",
    # Tracking and state
    "parse_tracking_ref",
    "fetch_github_issue_state",
    "fetch_linear_issue_state",
    "update_task_state",
    # Cache
    "get_cache_path",
    "load_cache",
    "save_cache",
    # URLs
    "extract_external_urls",
    "fetch_url_state",
]
