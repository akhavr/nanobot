"""Tests for Telegram state persistence utilities and config."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nanobot.channels.telegram import TelegramConfig
from nanobot.channels.telegram_state import (
    GroupMembersData,
    GroupOriginsData,
    _get_group_members_path,
    _get_group_origins_path,
    load_group_members,
    load_group_origins,
    save_group_members,
    save_group_origins,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect state files to a temp directory."""
    nanobot_dir = tmp_path / ".nanobot"
    nanobot_dir.mkdir()
    monkeypatch.setattr(
        "nanobot.channels.telegram_state._get_state_dir",
        lambda: nanobot_dir,
    )
    return nanobot_dir


class TestGroupOrigins:
    """Tests for group_origins.json persistence."""

    def test_load_returns_empty_dict_when_file_missing(self, state_dir: Path) -> None:
        result = load_group_origins()
        assert result == {}

    def test_save_creates_file_on_first_write(self, state_dir: Path) -> None:
        data: GroupOriginsData = {
            "123": {"added_by": 456, "added_at": 1700000000.0, "approved": True}
        }
        save_group_origins(data)

        path = _get_group_origins_path()
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == data

    def test_roundtrip(self, state_dir: Path) -> None:
        data: GroupOriginsData = {
            "-100123": {"added_by": 789, "added_at": 1700000000.5, "approved": False},
            "-100456": {"added_by": 111, "added_at": 1700001000.0, "approved": True},
        }
        save_group_origins(data)
        loaded = load_group_origins()
        assert loaded == data

    def test_load_returns_empty_dict_on_invalid_json(self, state_dir: Path) -> None:
        path = _get_group_origins_path()
        path.write_text("{not valid json", encoding="utf-8")
        result = load_group_origins()
        assert result == {}

    def test_load_returns_empty_dict_on_non_dict_json(self, state_dir: Path) -> None:
        path = _get_group_origins_path()
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = load_group_origins()
        assert result == {}

    def test_atomic_write_no_partial_corruption(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify atomic write doesn't corrupt existing file on failure."""
        data: GroupOriginsData = {
            "123": {"added_by": 456, "added_at": 1700000000.0, "approved": True}
        }
        save_group_origins(data)

        original_content = _get_group_origins_path().read_text(encoding="utf-8")

        real_replace = Path.replace

        def boom(self: Path, target: Path) -> None:
            raise OSError("simulated disk error")

        monkeypatch.setattr(Path, "replace", boom)

        with pytest.raises(OSError, match="simulated disk error"):
            save_group_origins({"new": {"added_by": 1, "added_at": 0.0, "approved": False}})

        monkeypatch.setattr(Path, "replace", real_replace)

        # Original file should be unchanged
        assert _get_group_origins_path().read_text(encoding="utf-8") == original_content


class TestGroupMembers:
    """Tests for group_members.json persistence."""

    def test_load_returns_empty_dict_when_file_missing(self, state_dir: Path) -> None:
        result = load_group_members()
        assert result == {}

    def test_save_creates_file_on_first_write(self, state_dir: Path) -> None:
        data: GroupMembersData = {"-100123": [111, 222, 333]}
        save_group_members(data)

        path = _get_group_members_path()
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == data

    def test_roundtrip(self, state_dir: Path) -> None:
        data: GroupMembersData = {
            "-100123": [111, 222],
            "-100456": [333, 444, 555],
        }
        save_group_members(data)
        loaded = load_group_members()
        assert loaded == data

    def test_load_returns_empty_dict_on_invalid_json(self, state_dir: Path) -> None:
        path = _get_group_members_path()
        path.write_text("not json at all", encoding="utf-8")
        result = load_group_members()
        assert result == {}

    def test_load_returns_empty_dict_on_non_dict_json(self, state_dir: Path) -> None:
        path = _get_group_members_path()
        path.write_text('"just a string"', encoding="utf-8")
        result = load_group_members()
        assert result == {}


class TestThreadSafety:
    """Tests for concurrent access."""

    def test_concurrent_writes_to_origins_dont_corrupt(self, state_dir: Path) -> None:
        """Multiple threads writing shouldn't corrupt the file."""
        results = []

        def write_entry(i: int) -> bool:
            data = load_group_origins()
            data[str(i)] = {"added_by": i, "added_at": time.time(), "approved": True}
            save_group_origins(data)
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(write_entry, i) for i in range(20)]
            results = [f.result() for f in futures]

        assert all(results)

        # File should still be valid JSON
        final_data = load_group_origins()
        assert isinstance(final_data, dict)
        # At least some entries should have been written
        assert len(final_data) > 0

    def test_concurrent_writes_to_members_dont_corrupt(self, state_dir: Path) -> None:
        """Multiple threads writing shouldn't corrupt the file."""

        def write_entry(i: int) -> bool:
            data = load_group_members()
            key = str(i % 5)  # 5 groups
            if key not in data:
                data[key] = []
            data[key].append(i)
            save_group_members(data)
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(write_entry, i) for i in range(20)]
            results = [f.result() for f in futures]

        assert all(results)

        # File should still be valid JSON
        final_data = load_group_members()
        assert isinstance(final_data, dict)


class TestTelegramConfig:
    """Tests for TelegramConfig group_allow_all field."""

    def test_group_allow_all_defaults_to_false(self) -> None:
        config = TelegramConfig(enabled=True, token="test:token")
        assert config.group_allow_all is False

    def test_group_allow_all_can_be_set_true(self) -> None:
        config = TelegramConfig(enabled=True, token="test:token", group_allow_all=True)
        assert config.group_allow_all is True

    def test_group_allow_all_parses_from_dict(self) -> None:
        data = {"enabled": True, "token": "test:token", "groupAllowAll": True}
        config = TelegramConfig.model_validate(data)
        assert config.group_allow_all is True
