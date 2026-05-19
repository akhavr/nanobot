"""Thread-safe state persistence for Telegram group management.

Provides utilities for persisting:
- group_origins.json: tracks group registration metadata
- group_members.json: tracks users seen in authorized groups
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TypedDict

from filelock import FileLock


class GroupOrigin(TypedDict):
    """Origin record for a registered group."""

    added_by: int
    added_at: float
    approved: bool


# Type aliases for clarity
GroupOriginsData = dict[str, GroupOrigin]
GroupMembersData = dict[str, list[int]]


def _get_state_dir() -> Path:
    """Return the .nanobot state directory."""
    return Path.home() / ".nanobot"


def _get_group_origins_path() -> Path:
    """Return path to group_origins.json."""
    return _get_state_dir() / "group_origins.json"


def _get_group_members_path() -> Path:
    """Return path to group_members.json."""
    return _get_state_dir() / "group_members.json"


def _get_lock_path(file_path: Path) -> Path:
    """Return lock file path for a given state file."""
    return file_path.with_suffix(".lock")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write data atomically using temp file + rename + fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    content = json.dumps(data, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)


def load_group_origins() -> GroupOriginsData:
    """Load group origins from disk with file locking.

    Returns:
        Dict mapping chat_id (str) to GroupOrigin with keys:
        - added_by: user_id who added the group
        - added_at: timestamp when added
        - approved: whether the group is approved

    Returns empty dict if file doesn't exist or is invalid.
    """
    path = _get_group_origins_path()
    lock = FileLock(_get_lock_path(path))
    with lock:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError):
            return {}


def save_group_origins(data: GroupOriginsData) -> None:
    """Save group origins to disk with file locking.

    Args:
        data: Dict mapping chat_id (str) to GroupOrigin
    """
    path = _get_group_origins_path()
    lock = FileLock(_get_lock_path(path))
    with lock:
        _atomic_write(path, data)


def load_group_members() -> GroupMembersData:
    """Load group members from disk with file locking.

    Returns:
        Dict mapping chat_id (str) to list of user_ids.
        Returns empty dict if file doesn't exist or is invalid.
    """
    path = _get_group_members_path()
    lock = FileLock(_get_lock_path(path))
    with lock:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError):
            return {}


def save_group_members(data: GroupMembersData) -> None:
    """Save group members to disk with file locking.

    Args:
        data: Dict mapping chat_id (str) to list of user_ids
    """
    path = _get_group_members_path()
    lock = FileLock(_get_lock_path(path))
    with lock:
        _atomic_write(path, data)
