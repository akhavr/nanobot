"""Tests for Telegram eval capture functionality."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    import telegram  # noqa: F401
except ImportError:
    pytest.skip("Telegram dependencies not installed", allow_module_level=True)

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel, TelegramConfig
from nanobot.config.schema import Config, EvalConfig


def _make_eval_config(group_id: str | None = None, enabled: bool = True) -> EvalConfig:
    return EvalConfig(group_id=group_id, enabled=enabled)


def _make_mock_message(
    chat_id: int,
    text: str | None = None,
    reply_to_message=None,
):
    return SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(type="group", title="Test Group"),
        text=text,
        caption=None,
        message_id=1,
        reply_to_message=reply_to_message,
    )


def _make_forwarded_message(
    forward_from_chat_id: int,
    forward_date: datetime,
    text: str | None = None,
):
    return SimpleNamespace(
        text=text,
        caption=None,
        forward_from_chat=SimpleNamespace(id=forward_from_chat_id),
        forward_date=forward_date,
    )


@pytest.fixture
def temp_workspace(tmp_path: Path):
    """Create a temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = workspace / "sessions"
    sessions.mkdir()
    return workspace


@pytest.fixture
def telegram_channel():
    """Create a TelegramChannel for testing."""
    config = TelegramConfig(enabled=True, token="test:token", allow_from=["*"])
    bus = MessageBus()
    return TelegramChannel(config, bus)


@pytest.mark.asyncio
async def test_try_eval_capture_disabled(telegram_channel):
    """Eval capture returns False when disabled."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(enabled=False)

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="test")
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_no_group_id(telegram_channel):
    """Eval capture returns False when no group_id configured."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id=None)

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="test")
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_wrong_group(telegram_channel):
    """Eval capture returns False when message is not in eval group."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-999")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="test")
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_no_reply(telegram_channel):
    """Eval capture returns False when message is not a reply."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="test", reply_to_message=None)
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_reply_not_forwarded(telegram_channel):
    """Eval capture returns False when reply is not to a forwarded message."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")

    reply = SimpleNamespace(
        text="some reply",
        caption=None,
        forward_from_chat=None,
        forward_date=None,
    )

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="explanation", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_empty_explanation(telegram_channel):
    """Eval capture returns False when explanation is empty."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")

    forward_date = datetime.now(timezone.utc)
    reply = _make_forwarded_message(
        forward_from_chat_id=-456,
        forward_date=forward_date,
        text="bad response",
    )

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_try_eval_capture_success(telegram_channel, temp_workspace):
    """Eval capture succeeds and stores entry."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_date = datetime.now(timezone.utc)
    reply = _make_forwarded_message(
        forward_from_chat_id=-456,
        forward_date=forward_date,
        text="This is a bad response from the bot",
    )

    sessions_dir = temp_workspace / "sessions"
    session_file = sessions_dir / "telegram_-456.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:-456", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "assistant", "content": "This is a bad response from the bot",
         "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(
            chat_id=-123,
            text="The bot got the gender wrong here",
            reply_to_message=reply,
        )
        result = await telegram_channel._try_eval_capture(message)
        assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()

    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["original_chat_id"] == "-456"
    assert entry["bad_message"] == "This is a bad response from the bot"
    assert entry["explanation"] == "The bot got the gender wrong here"
    assert "timestamp" in entry
    assert "original_timestamp" in entry


@pytest.mark.asyncio
async def test_handle_eval_capture_with_session_context(telegram_channel, temp_workspace):
    """Eval capture extracts context from session file."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    sessions_dir = temp_workspace / "sessions"
    session_file = sessions_dir / "telegram_-456.jsonl"
    forward_time = datetime.now(timezone.utc)

    session_messages = [
        {"_type": "metadata", "key": "telegram:-456", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "Hello bot", "timestamp": (forward_time.isoformat())},
        {"role": "assistant", "content": "Hi there!", "timestamp": forward_time.isoformat()},
        {"role": "user", "content": "What is X?", "timestamp": forward_time.isoformat()},
        {"role": "assistant", "content": "Bad answer here", "timestamp": forward_time.isoformat()},
    ]

    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        await telegram_channel._handle_eval_capture(
            original_chat_id="-456",
            forward_date=forward_time,
            bad_message_text="Bad answer here",
            explanation="This answer is wrong",
        )

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert len(entry["context"]) > 0
    assert any(c["content"] == "Hello bot" for c in entry["context"])


@pytest.mark.asyncio
async def test_handle_eval_capture_no_session_skips(telegram_channel, temp_workspace):
    """Eval capture skips when session file doesn't exist."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_time = datetime.now(timezone.utc)

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        await telegram_channel._handle_eval_capture(
            original_chat_id="-456",
            forward_date=forward_time,
            bad_message_text="Bad answer here",
            explanation="This answer is wrong",
        )

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert not feedback_path.exists(), "No entry should be written when session not found"
