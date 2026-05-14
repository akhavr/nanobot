"""Tests for filename <-> session key conversion symmetry."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.session.manager import SessionManager


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return tmp_path


@pytest.fixture
def manager(sessions_dir: Path) -> SessionManager:
    return SessionManager(workspace=sessions_dir)


class TestFilenameKeyConversion:
    """Verify that key->filename->key roundtrips correctly."""

    def test_simple_key_roundtrip(self, manager: SessionManager):
        """Simple key with one colon roundtrips correctly."""
        original_key = "telegram:12345"
        session = manager.get_or_create(original_key)
        session.add_message("user", "hello")
        manager.save(session)

        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["key"] == original_key

    def test_topic_key_roundtrip(self, manager: SessionManager):
        """Topic session key with multiple colons roundtrips correctly.

        This is the regression test for the bug where only the first
        underscore was converted back to colon.
        """
        original_key = "telegram:-1001234567890:topic:123"
        session = manager.get_or_create(original_key)
        session.add_message("user", "hello from topic")
        manager.save(session)

        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["key"] == original_key

    def test_filename_without_key_in_metadata_extracts_key_correctly(
        self, manager: SessionManager, sessions_dir: Path
    ):
        """A file with metadata but no 'key' field extracts key from filename.

        This tests the fallback path where we derive key from filename.
        """
        import json

        # Create a file directly without going through SessionManager
        sessions_path = sessions_dir / "sessions"
        filename = "telegram_-1001234567890_topic_456.jsonl"
        session_file = sessions_path / filename
        # Write metadata without a key field - forces fallback to filename
        metadata_line = json.dumps({
            "_type": "metadata",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        })
        message_line = json.dumps({"role": "user", "content": "test"})
        session_file.write_text(f"{metadata_line}\n{message_line}\n")

        sessions = manager.list_sessions()
        assert len(sessions) == 1
        # All underscores should become colons
        expected_key = "telegram:-1001234567890:topic:456"
        assert sessions[0]["key"] == expected_key
