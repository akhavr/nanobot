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
from nanobot.config.schema import EvalConfig


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


def _make_forward_origin_user(user_id: int, forward_date: datetime):
    """Create a MessageOriginUser-like object for testing."""
    return SimpleNamespace(
        date=forward_date,
        sender_user=SimpleNamespace(id=user_id),
    )


def _make_forward_origin_chat(chat_id: int, forward_date: datetime):
    """Create a MessageOriginChat-like object for testing."""
    return SimpleNamespace(
        date=forward_date,
        sender_chat=SimpleNamespace(id=chat_id),
    )


def _make_forward_origin_channel(chat_id: int, forward_date: datetime):
    """Create a MessageOriginChannel-like object for testing."""
    return SimpleNamespace(
        date=forward_date,
        chat=SimpleNamespace(id=chat_id),
    )


def _make_forward_origin_hidden_user(forward_date: datetime):
    """Create a MessageOriginHiddenUser-like object for testing."""
    return SimpleNamespace(
        date=forward_date,
    )


def _make_forwarded_message_with_origin(forward_origin, text: str | None = None):
    """Create a forwarded message using forward_origin (new API)."""
    return SimpleNamespace(
        text=text,
        caption=None,
        forward_origin=forward_origin,
    )


def _make_forwarded_message(
    forward_from_chat_id: int,
    forward_date: datetime,
    text: str | None = None,
):
    """Legacy helper - creates message with forward_origin for compatibility."""
    forward_origin = _make_forward_origin_chat(forward_from_chat_id, forward_date)
    return _make_forwarded_message_with_origin(forward_origin, text)


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
        forward_origin=None,
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


# ---------------------------------------------------------------------------
# Tests for forward_origin API (python-telegram-bot v22.7+)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_capture_message_origin_user(telegram_channel, temp_workspace):
    """Eval capture works with MessageOriginUser (forwarded from DM)."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_date = datetime.now(timezone.utc)
    forward_origin = _make_forward_origin_user(user_id=456, forward_date=forward_date)
    reply = _make_forwarded_message_with_origin(forward_origin, text="Bad bot response")

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "telegram_456.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:456", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "assistant", "content": "Bad bot response", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="Wrong answer", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["original_chat_id"] == "456"


@pytest.mark.asyncio
async def test_eval_capture_message_origin_chat(telegram_channel, temp_workspace):
    """Eval capture works with MessageOriginChat (forwarded from group)."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_date = datetime.now(timezone.utc)
    forward_origin = _make_forward_origin_chat(chat_id=-456, forward_date=forward_date)
    reply = _make_forwarded_message_with_origin(forward_origin, text="Bad bot response")

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "telegram_-456.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:-456", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "assistant", "content": "Bad bot response", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="Wrong answer", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["original_chat_id"] == "-456"


@pytest.mark.asyncio
async def test_eval_capture_message_origin_channel(telegram_channel, temp_workspace):
    """Eval capture works with MessageOriginChannel (forwarded from channel)."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_date = datetime.now(timezone.utc)
    forward_origin = _make_forward_origin_channel(chat_id=-789, forward_date=forward_date)
    reply = _make_forwarded_message_with_origin(forward_origin, text="Bad bot response")

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "telegram_-789.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:-789", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "assistant", "content": "Bad bot response", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="Wrong answer", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["original_chat_id"] == "-789"


@pytest.mark.asyncio
async def test_eval_capture_message_origin_hidden_user(telegram_channel):
    """Eval capture gracefully skips MessageOriginHiddenUser (no identifiable source)."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")

    forward_date = datetime.now(timezone.utc)
    forward_origin = _make_forward_origin_hidden_user(forward_date=forward_date)
    reply = _make_forwarded_message_with_origin(forward_origin, text="Bad bot response")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="Wrong answer", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_eval_capture_no_forward_origin(telegram_channel):
    """Eval capture returns False when reply has no forward_origin."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")

    reply = SimpleNamespace(
        text="some reply",
        caption=None,
        forward_origin=None,
    )

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(chat_id=-123, text="explanation", reply_to_message=reply)
        result = await telegram_channel._try_eval_capture(message)
        assert result is False


@pytest.mark.asyncio
async def test_eval_capture_stores_feedback(telegram_channel, temp_workspace):
    """Eval capture stores feedback entry with correct fields."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-123")
    mock_config.workspace_path = temp_workspace

    forward_date = datetime.now(timezone.utc)
    forward_origin = _make_forward_origin_chat(chat_id=-456, forward_date=forward_date)
    reply = _make_forwarded_message_with_origin(forward_origin, text="This is a bad response")

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "telegram_-456.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:-456", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "What is 2+2?", "timestamp": forward_date.isoformat()},
        {"role": "assistant", "content": "This is a bad response", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(
            chat_id=-123,
            text="The bot got this completely wrong",
            reply_to_message=reply,
        )
        result = await telegram_channel._try_eval_capture(message)
        assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()

    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["original_chat_id"] == "-456"
    assert entry["bad_message"] == "This is a bad response"
    assert entry["explanation"] == "The bot got this completely wrong"
    assert "timestamp" in entry
    assert "original_timestamp" in entry
    assert "context" in entry
    assert "session_file" in entry


@pytest.mark.asyncio
async def test_eval_capture_e2e(telegram_channel, temp_workspace):
    """E2E test: forward bot message to eval group, reply with explanation, verify feedback.jsonl."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-100")
    mock_config.workspace_path = temp_workspace

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "telegram_-555.jsonl"
    forward_date = datetime.now(timezone.utc)
    session_messages = [
        {"_type": "metadata", "key": "telegram:-555", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "What is the capital of France?", "timestamp": forward_date.isoformat()},
        {"role": "assistant", "content": "The capital of France is Berlin.", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    forward_origin = _make_forward_origin_chat(chat_id=-555, forward_date=forward_date)
    forwarded_bot_message = _make_forwarded_message_with_origin(
        forward_origin, text="The capital of France is Berlin."
    )

    evaluator_reply = _make_mock_message(
        chat_id=-100,
        text="Wrong! The capital of France is Paris, not Berlin.",
        reply_to_message=forwarded_bot_message,
    )

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        result = await telegram_channel._try_eval_capture(evaluator_reply)

    assert result is True, "Eval capture should return True for valid eval reply"

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists(), "feedback.jsonl should be created"

    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["original_chat_id"] == "-555"
    assert entry["bad_message"] == "The capital of France is Berlin."
    assert entry["explanation"] == "Wrong! The capital of France is Paris, not Berlin."
    assert "timestamp" in entry
    assert "original_timestamp" in entry
    assert "context" in entry
    assert len(entry["context"]) >= 1
    assert any(c["content"] == "What is the capital of France?" for c in entry["context"])


# ---------------------------------------------------------------------------
# Tests for message-to-session index (Tier 1) and content search fallback (Tier 2)
# ---------------------------------------------------------------------------


def test_message_index_store_on_send(temp_workspace):
    """When bot sends message, entry is added to message_index.json."""
    from nanobot.channels.telegram import MessageIndex

    index = MessageIndex(temp_workspace)
    index.store("telegram:12345", "-100123", "Hello, how can I help?")

    entries = index.load_all()
    assert len(entries) == 1
    assert entries[0]["session_key"] == "telegram:12345"
    assert entries[0]["chat_id"] == "-100123"
    assert "text_hash" in entries[0]
    assert "ts" in entries[0]


def test_message_index_lookup_by_timestamp(temp_workspace):
    """Find session by forward_origin.date with ±5s tolerance."""
    from datetime import timedelta
    from nanobot.channels.telegram import MessageIndex

    index = MessageIndex(temp_workspace)
    send_time = datetime.now(timezone.utc)
    index.store("telegram:12345", "-100123", "Test message", send_time)

    lookup_time = send_time + timedelta(seconds=3)
    result = index.lookup_by_timestamp(lookup_time, tolerance_seconds=5)
    assert result is not None
    assert result["session_key"] == "telegram:12345"

    lookup_time_outside = send_time + timedelta(seconds=10)
    result_outside = index.lookup_by_timestamp(lookup_time_outside, tolerance_seconds=5)
    assert result_outside is None


def test_message_index_fifo_eviction(temp_workspace):
    """Index stays under 1000 entries via FIFO eviction."""
    from nanobot.channels.telegram import MessageIndex

    index = MessageIndex(temp_workspace, max_entries=10)
    for i in range(15):
        index.store(f"telegram:{i}", "-100123", f"Message {i}")

    entries = index.load_all()
    assert len(entries) == 10
    session_keys = [e["session_key"] for e in entries]
    assert "telegram:0" not in session_keys
    assert "telegram:14" in session_keys


def test_content_search_fallback(temp_workspace):
    """When index miss, search sessions by text hash."""
    from nanobot.channels.telegram import content_search_sessions

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions_dir / "telegram_12345.jsonl"
    session_messages = [
        {"_type": "metadata", "key": "telegram:12345", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "Hello", "timestamp": "2024-01-01T00:01:00Z"},
        {"role": "assistant", "content": "This is the bad message to find", "timestamp": "2024-01-01T00:01:30Z"},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    result = content_search_sessions(temp_workspace, "This is the bad message to find")
    assert result is not None
    assert result == "telegram:12345"


def test_content_search_finds_match(temp_workspace):
    """Content search returns correct session when text matches."""
    from nanobot.channels.telegram import content_search_sessions

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    for i in range(3):
        session_file = sessions_dir / f"telegram_{i}.jsonl"
        content = f"Unique content {i}" if i != 1 else "This is the target message"
        with open(session_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": f"telegram:{i}"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": content}) + "\n")

    result = content_search_sessions(temp_workspace, "This is the target message")
    assert result == "telegram:1"


@pytest.mark.asyncio
async def test_eval_capture_uses_tiered_lookup(telegram_channel, temp_workspace):
    """Capture tries index first, then content search when sender_user.id is bot ID."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-100")
    mock_config.workspace_path = temp_workspace

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions_dir / "telegram_12345.jsonl"
    forward_date = datetime.now(timezone.utc)
    session_messages = [
        {"_type": "metadata", "key": "telegram:12345", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "What is 2+2?", "timestamp": forward_date.isoformat()},
        {"role": "assistant", "content": "The answer is 5", "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    from nanobot.channels.telegram import MessageIndex
    index = MessageIndex(temp_workspace)
    index.store("telegram:12345", "-100999", "The answer is 5", forward_date)

    forward_origin = _make_forward_origin_user(user_id=999999, forward_date=forward_date)
    forwarded_msg = _make_forwarded_message_with_origin(forward_origin, text="The answer is 5")

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        message = _make_mock_message(
            chat_id=-100,
            text="Wrong! 2+2=4",
            reply_to_message=forwarded_msg,
        )
        result = await telegram_channel._try_eval_capture(message)

    assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["original_chat_id"] == "12345"


@pytest.mark.asyncio
async def test_eval_capture_full_flow_with_index(telegram_channel, temp_workspace):
    """E2E: Bot sends → forward to eval group → reply → captured with correct session via index."""
    mock_config = MagicMock()
    mock_config.eval = _make_eval_config(group_id="-100")
    mock_config.workspace_path = temp_workspace

    sessions_dir = temp_workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    user_chat_id = "334424084"
    session_file = sessions_dir / f"telegram_{user_chat_id}.jsonl"
    forward_date = datetime.now(timezone.utc)
    bot_response = "I think the sky is green."

    session_messages = [
        {"_type": "metadata", "key": f"telegram:{user_chat_id}", "created_at": "2024-01-01T00:00:00Z"},
        {"role": "user", "content": "What color is the sky?", "timestamp": forward_date.isoformat()},
        {"role": "assistant", "content": bot_response, "timestamp": forward_date.isoformat()},
    ]
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in session_messages:
            f.write(json.dumps(msg) + "\n")

    from nanobot.channels.telegram import MessageIndex
    index = MessageIndex(temp_workspace)
    index.store(f"telegram:{user_chat_id}", user_chat_id, bot_response, forward_date)

    bot_user_id = 6836135386
    forward_origin = _make_forward_origin_user(user_id=bot_user_id, forward_date=forward_date)
    forwarded_msg = _make_forwarded_message_with_origin(forward_origin, text=bot_response)

    evaluator_reply = _make_mock_message(
        chat_id=-100,
        text="The sky is blue, not green!",
        reply_to_message=forwarded_msg,
    )

    with patch("nanobot.config.loader.load_config", return_value=mock_config):
        result = await telegram_channel._try_eval_capture(evaluator_reply)

    assert result is True

    feedback_path = temp_workspace / "evals" / "feedback.jsonl"
    assert feedback_path.exists()
    with open(feedback_path, "r", encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["original_chat_id"] == user_chat_id
    assert entry["bad_message"] == bot_response
    assert entry["explanation"] == "The sky is blue, not green!"
    assert len(entry["context"]) >= 1
