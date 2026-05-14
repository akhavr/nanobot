import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Check optional Telegram dependencies before running tests
try:
    import telegram  # noqa: F401
except ImportError:
    pytest.skip("Telegram dependencies not installed (python-telegram-bot)", allow_module_level=True)

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import (
    TELEGRAM_REPLY_CONTEXT_MAX_LEN,
    TelegramChannel,
    TelegramConfig,
    _format_relative_time,
    _load_groups_data,
    _prune_old_seen_groups,
    _save_groups_data,
    _StreamBuf,
)


class _FakeHTTPXRequest:
    instances: list["_FakeHTTPXRequest"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    @classmethod
    def clear(cls) -> None:
        cls.instances.clear()


class _FakeUpdater:
    def __init__(self, on_start_polling) -> None:
        self._on_start_polling = on_start_polling
        self.start_polling_kwargs = None

    async def start_polling(self, **kwargs) -> None:
        self.start_polling_kwargs = kwargs
        self._on_start_polling()


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.sent_media: list[dict] = []
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        return SimpleNamespace(id=999, username="nanobot_test")

    async def set_my_commands(self, commands) -> None:
        self.commands = commands

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_messages))

    async def send_photo(self, **kwargs) -> None:
        self.sent_media.append({"kind": "photo", **kwargs})

    async def send_video(self, **kwargs) -> None:
        self.sent_media.append({"kind": "video", **kwargs})

    async def send_voice(self, **kwargs) -> None:
        self.sent_media.append({"kind": "voice", **kwargs})

    async def send_audio(self, **kwargs) -> None:
        self.sent_media.append({"kind": "audio", **kwargs})

    async def send_document(self, **kwargs) -> None:
        self.sent_media.append({"kind": "document", **kwargs})

    async def send_chat_action(self, **kwargs) -> None:
        pass

    async def get_file(self, file_id: str):
        """Return a fake file that 'downloads' to a path (for reply-to-media tests)."""
        async def _fake_download(path) -> None:
            pass
        return SimpleNamespace(download_to_drive=_fake_download)


class _FakeApp:
    def __init__(self, on_start_polling) -> None:
        self.bot = _FakeBot()
        self.updater = _FakeUpdater(on_start_polling)
        self.handlers = []
        self.error_handlers = []

    def add_error_handler(self, handler) -> None:
        self.error_handlers.append(handler)

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        pass

    async def start(self) -> None:
        pass


class _FakeBuilder:
    def __init__(self, app: _FakeApp) -> None:
        self.app = app
        self.token_value = None
        self.request_value = None
        self.get_updates_request_value = None

    def token(self, token: str):
        self.token_value = token
        return self

    def request(self, request):
        self.request_value = request
        return self

    def get_updates_request(self, request):
        self.get_updates_request_value = request
        return self

    def proxy(self, _proxy):
        raise AssertionError("builder.proxy should not be called when request is set")

    def get_updates_proxy(self, _proxy):
        raise AssertionError("builder.get_updates_proxy should not be called when request is set")

    def build(self):
        return self.app


def _make_telegram_update(
    *,
    chat_type: str = "group",
    text: str | None = None,
    caption: str | None = None,
    entities=None,
    caption_entities=None,
    reply_to_message=None,
    location=None,
):
    async def mock_get_member_count():
        return 2

    user = SimpleNamespace(id=12345, username="alice", first_name="Alice")
    chat = SimpleNamespace(
        type=chat_type,
        is_forum=False,
        id=-100123,
        get_member_count=mock_get_member_count,
    )
    message = SimpleNamespace(
        chat=chat,
        chat_id=-100123,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        reply_to_message=reply_to_message,
        photo=None,
        voice=None,
        audio=None,
        document=None,
        location=location,
        media_group_id=None,
        message_thread_id=None,
        message_id=1,
    )
    return SimpleNamespace(message=message, effective_user=user)


@pytest.mark.asyncio
async def test_start_creates_separate_pools_with_proxy(monkeypatch) -> None:
    _FakeHTTPXRequest.clear()
    config = TelegramConfig(
        enabled=True,
        token="123:abc",
        allow_from=["*"],
        proxy="http://127.0.0.1:7890",
    )
    bus = MessageBus()
    channel = TelegramChannel(config, bus)
    app = _FakeApp(lambda: setattr(channel, "_running", False))
    builder = _FakeBuilder(app)

    monkeypatch.setattr("nanobot.channels.telegram.HTTPXRequest", _FakeHTTPXRequest)
    monkeypatch.setattr(
        "nanobot.channels.telegram.Application",
        SimpleNamespace(builder=lambda: builder),
    )

    await channel.start()

    assert len(_FakeHTTPXRequest.instances) == 2
    api_req, poll_req = _FakeHTTPXRequest.instances
    assert api_req.kwargs["proxy"] == config.proxy
    assert poll_req.kwargs["proxy"] == config.proxy
    assert api_req.kwargs["connection_pool_size"] == 32
    assert poll_req.kwargs["connection_pool_size"] == 4
    assert builder.request_value is api_req
    assert builder.get_updates_request_value is poll_req
    assert callable(app.updater.start_polling_kwargs["error_callback"])
    assert any(cmd.command == "status" for cmd in app.bot.commands)
    assert any(cmd.command == "history" for cmd in app.bot.commands)
    assert any(cmd.command == "dream" for cmd in app.bot.commands)
    assert any(cmd.command == "dream_log" for cmd in app.bot.commands)
    assert any(cmd.command == "dream_restore" for cmd in app.bot.commands)


@pytest.mark.asyncio
async def test_start_respects_custom_pool_config(monkeypatch) -> None:
    _FakeHTTPXRequest.clear()
    config = TelegramConfig(
        enabled=True,
        token="123:abc",
        allow_from=["*"],
        connection_pool_size=32,
        pool_timeout=10.0,
    )
    bus = MessageBus()
    channel = TelegramChannel(config, bus)
    app = _FakeApp(lambda: setattr(channel, "_running", False))
    builder = _FakeBuilder(app)

    monkeypatch.setattr("nanobot.channels.telegram.HTTPXRequest", _FakeHTTPXRequest)
    monkeypatch.setattr(
        "nanobot.channels.telegram.Application",
        SimpleNamespace(builder=lambda: builder),
    )

    await channel.start()

    api_req = _FakeHTTPXRequest.instances[0]
    poll_req = _FakeHTTPXRequest.instances[1]
    assert api_req.kwargs["connection_pool_size"] == 32
    assert api_req.kwargs["pool_timeout"] == 10.0
    assert poll_req.kwargs["pool_timeout"] == 10.0


@pytest.mark.asyncio
async def test_send_text_retries_on_timeout() -> None:
    """_send_text retries on TimedOut before succeeding."""
    from telegram.error import TimedOut

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    call_count = 0
    original_send = channel._app.bot.send_message

    async def flaky_send(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise TimedOut()
        return await original_send(**kwargs)

    channel._app.bot.send_message = flaky_send

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        await channel._send_text(123, "hello", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    assert call_count == 3
    assert len(channel._app.bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_send_text_gives_up_after_max_retries() -> None:
    """_send_text raises TimedOut after exhausting all retries."""
    from telegram.error import TimedOut

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    async def always_timeout(**kwargs):
        raise TimedOut()

    channel._app.bot.send_message = always_timeout

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        with pytest.raises(TimedOut):
            await channel._send_text(123, "hello", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    assert channel._app.bot.sent_messages == []


@pytest.mark.asyncio
async def test_on_error_logs_network_issues_as_warning(monkeypatch) -> None:
    from telegram.error import NetworkError

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(
        channel.logger,
        "warning",
        lambda message, error: recorded.append(("warning", message.format(error))),
    )
    monkeypatch.setattr(
        channel.logger,
        "error",
        lambda message, error: recorded.append(("error", message.format(error))),
    )

    await channel._on_error(object(), SimpleNamespace(error=NetworkError("proxy disconnected")))

    assert recorded == [("warning", "network issue: proxy disconnected")]


@pytest.mark.asyncio
async def test_on_error_summarizes_empty_network_error(monkeypatch) -> None:
    from telegram.error import NetworkError

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(
        channel.logger,
        "warning",
        lambda message, error: recorded.append(("warning", message.format(error))),
    )

    await channel._on_error(object(), SimpleNamespace(error=NetworkError("")))

    assert recorded == [("warning", "network issue: NetworkError")]


@pytest.mark.asyncio
async def test_on_error_keeps_non_network_exceptions_as_error(monkeypatch) -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(
        channel.logger,
        "warning",
        lambda message, error: recorded.append(("warning", message.format(error))),
    )
    monkeypatch.setattr(
        channel.logger,
        "error",
        lambda message, error: recorded.append(("error", message.format(error))),
    )

    await channel._on_error(object(), SimpleNamespace(error=RuntimeError("boom")))

    assert recorded == [("error", "error: boom")]


@pytest.mark.asyncio
async def test_send_delta_stream_end_raises_and_keeps_buffer_on_failure() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock(side_effect=RuntimeError("boom"))
    channel._stream_bufs["123"] = _StreamBuf(text="hello", message_id=7, last_edit=0.0)

    with pytest.raises(RuntimeError, match="boom"):
        await channel.send_delta("123", "", {"_stream_end": True})

    assert "123" in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_treats_not_modified_as_success() -> None:
    from telegram.error import BadRequest

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    channel._stream_bufs["123"] = _StreamBuf(text="hello", message_id=7, last_edit=0.0, stream_id="s:0")

    await channel.send_delta("123", "", {"_stream_end": True, "_stream_id": "s:0"})

    assert "123" not in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_does_not_fallback_on_network_timeout() -> None:
    """TimedOut during HTML edit should propagate, never fall back to plain text."""
    from telegram.error import TimedOut

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    # _call_with_retry retries TimedOut up to 3 times, so the mock will be called
    # multiple times – but all calls must be with parse_mode="HTML" (no plain fallback).
    channel._app.bot.edit_message_text = AsyncMock(side_effect=TimedOut("network timeout"))
    channel._stream_bufs["123"] = _StreamBuf(text="hello", message_id=7, last_edit=0.0)

    with pytest.raises(TimedOut, match="network timeout"):
        await channel.send_delta("123", "", {"_stream_end": True})

    # Every call to edit_message_text must have used parse_mode="HTML" —
    # no plain-text fallback call should have been made.
    for call in channel._app.bot.edit_message_text.call_args_list:
        assert call.kwargs.get("parse_mode") == "HTML"
    # Buffer should still be present (not cleaned up on error)
    assert "123" in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_does_not_fallback_on_network_error() -> None:
    """NetworkError during HTML edit should propagate, never fall back to plain text."""
    from telegram.error import NetworkError

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock(side_effect=NetworkError("connection reset"))
    channel._stream_bufs["123"] = _StreamBuf(text="hello", message_id=7, last_edit=0.0)

    with pytest.raises(NetworkError, match="connection reset"):
        await channel.send_delta("123", "", {"_stream_end": True})

    # Every call to edit_message_text must have used parse_mode="HTML" —
    # no plain-text fallback call should have been made.
    for call in channel._app.bot.edit_message_text.call_args_list:
        assert call.kwargs.get("parse_mode") == "HTML"
    # Buffer should still be present (not cleaned up on error)
    assert "123" in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_falls_back_on_bad_request() -> None:
    """BadRequest (HTML parse error) should still trigger plain-text fallback."""
    from telegram.error import BadRequest

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    # First call (HTML) raises BadRequest, second call (plain) succeeds
    channel._app.bot.edit_message_text = AsyncMock(
        side_effect=[BadRequest("Can't parse entities"), None]
    )
    channel._stream_bufs["123"] = _StreamBuf(text="hello <bad>", message_id=7, last_edit=0.0)

    await channel.send_delta("123", "", {"_stream_end": True})

    # edit_message_text should have been called twice: once for HTML, once for plain fallback
    assert channel._app.bot.edit_message_text.call_count == 2
    # Second call should not use parse_mode="HTML"
    second_call_kwargs = channel._app.bot.edit_message_text.call_args_list[1].kwargs
    assert "parse_mode" not in second_call_kwargs or second_call_kwargs.get("parse_mode") is None
    # Buffer should be cleaned up on success
    assert "123" not in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_splits_oversized_reply() -> None:
    """Final streamed reply exceeding Telegram limit is split into chunks.

    The fix converts markdown to HTML first, then splits by 4096 (actual Telegram
    limit), ensuring the edited message always fits within Telegram's constraint.
    Previously, the code split by 4000 (TELEGRAM_MAX_MESSAGE_LEN) before HTML
    conversion, which could still overflow when HTML tags were added.
    """
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock()
    channel._app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))

    oversized = "x" * (4000 + 500)
    channel._stream_bufs["123"] = _StreamBuf(text=oversized, message_id=7, last_edit=0.0)

    await channel.send_delta("123", "", {"_stream_end": True})

    channel._app.bot.edit_message_text.assert_called_once()
    edit_text = channel._app.bot.edit_message_text.call_args.kwargs.get("text", "")
    assert len(edit_text) <= 4096, f"edit_text length {len(edit_text)} exceeds Telegram's 4096 limit"

    channel._app.bot.send_message.assert_called_once()
    send_text = channel._app.bot.send_message.call_args.kwargs.get("text", "")
    assert len(send_text) <= 4096
    assert "123" not in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_stream_end_html_expansion_does_not_overflow() -> None:
    """Markdown that expands when converted to HTML is still split correctly.

    This is the actual bug from issue #3315: markdown like **bold** expands to
    <b>bold</b>, adding ~33% characters. A 3600-char message with heavy markdown
    could become 4800+ chars after HTML conversion, exceeding 4096 limit.
    The fix converts to HTML first, THEN splits by 4096.
    """
    from nanobot.channels.telegram import _markdown_to_telegram_html

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock()
    channel._app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))

    markdown_text = "**bold** " * 400  # 3600 chars raw, expands ~33% to 4800 HTML
    raw_len = len(markdown_text)
    html_len = len(_markdown_to_telegram_html(markdown_text))
    assert html_len > 4096, f"Test precondition failed: HTML should exceed 4096 (was {html_len})"

    channel._stream_bufs["123"] = _StreamBuf(text=markdown_text, message_id=7, last_edit=0.0)

    await channel.send_delta("123", "", {"_stream_end": True})

    channel._app.bot.edit_message_text.assert_called_once()
    edit_text = channel._app.bot.edit_message_text.call_args.kwargs.get("text", "")
    assert len(edit_text) <= 4096, (
        f"HTML text length {len(edit_text)} exceeds Telegram's 4096 limit. "
        f"Raw was {raw_len}, HTML was {html_len}."
    )

    channel._app.bot.send_message.assert_called_once()
    assert "123" not in channel._stream_bufs


@pytest.mark.asyncio
async def test_send_delta_new_stream_id_replaces_stale_buffer() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._stream_bufs["123"] = _StreamBuf(
        text="hello",
        message_id=7,
        last_edit=0.0,
        stream_id="old:0",
    )

    await channel.send_delta("123", "world", {"_stream_delta": True, "_stream_id": "new:0"})

    buf = channel._stream_bufs["123"]
    assert buf.text == "world"
    assert buf.stream_id == "new:0"
    assert buf.message_id == 1


@pytest.mark.asyncio
async def test_send_delta_incremental_edit_treats_not_modified_as_success() -> None:
    from telegram.error import BadRequest

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._stream_bufs["123"] = _StreamBuf(text="hello", message_id=7, last_edit=0.0, stream_id="s:0")
    channel._app.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))

    await channel.send_delta("123", "", {"_stream_delta": True, "_stream_id": "s:0"})

    assert channel._stream_bufs["123"].last_edit > 0.0


@pytest.mark.asyncio
async def test_send_delta_incremental_edit_splits_oversized_buffer() -> None:
    """Mid-stream overflow: once buf.text exceeds Telegram's limit, split into
    chunks, edit the current message with the first chunk, and re-anchor the
    buffer to a new message for the tail so further deltas keep streaming."""
    from nanobot.channels.telegram import TELEGRAM_MAX_MESSAGE_LEN

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.edit_message_text = AsyncMock()
    channel._app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))

    oversized = "x" * (TELEGRAM_MAX_MESSAGE_LEN + 500)
    channel._stream_bufs["123"] = _StreamBuf(
        text=oversized, message_id=7, last_edit=0.0, stream_id="s:0"
    )

    await channel.send_delta("123", "y", {"_stream_delta": True, "_stream_id": "s:0"})

    channel._app.bot.edit_message_text.assert_called_once()
    edit_text = channel._app.bot.edit_message_text.call_args.kwargs.get("text", "")
    assert len(edit_text) <= TELEGRAM_MAX_MESSAGE_LEN

    channel._app.bot.send_message.assert_called_once()
    buf = channel._stream_bufs["123"]
    assert buf.message_id == 99
    assert len(buf.text) <= TELEGRAM_MAX_MESSAGE_LEN
    assert buf.last_edit > 0.0


@pytest.mark.asyncio
async def test_send_delta_initial_send_keeps_message_in_thread() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    await channel.send_delta(
        "123",
        "hello",
        {"_stream_delta": True, "_stream_id": "s:0", "message_thread_id": 42},
    )

    assert channel._app.bot.sent_messages[0]["message_thread_id"] == 42


def test_derive_topic_session_key_uses_thread_id() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type="supergroup"),
        chat_id=-100123,
        message_thread_id=42,
    )

    assert TelegramChannel._derive_topic_session_key(message) == "telegram:-100123:topic:42"


def test_derive_topic_session_key_private_dm_thread() -> None:
    """Private DM threads (Telegram Threaded Mode) must get their own session key."""
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        chat_id=999,
        message_thread_id=7,
    )
    assert TelegramChannel._derive_topic_session_key(message) == "telegram:999:topic:7"


def test_derive_topic_session_key_none_without_thread() -> None:
    """No thread id → no topic session key, regardless of chat type."""
    for chat_type in ("private", "supergroup", "group"):
        message = SimpleNamespace(
            chat=SimpleNamespace(type=chat_type),
            chat_id=123,
            message_thread_id=None,
        )
        assert TelegramChannel._derive_topic_session_key(message) is None


def test_get_extension_falls_back_to_original_filename() -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())

    assert channel._get_extension("file", None, "report.pdf") == ".pdf"
    assert channel._get_extension("file", None, "archive.tar.gz") == ".tar.gz"


def test_telegram_group_policy_defaults_to_mention() -> None:
    assert TelegramConfig().group_policy == "mention"


def test_is_allowed_accepts_legacy_telegram_id_username_formats() -> None:
    channel = TelegramChannel(TelegramConfig(allow_from=["12345", "alice", "67890|bob"]), MessageBus())

    assert channel.is_allowed("12345|carol") is True
    assert channel.is_allowed("99999|alice") is True
    assert channel.is_allowed("67890|bob") is True


def test_is_allowed_rejects_invalid_legacy_telegram_sender_shapes() -> None:
    channel = TelegramChannel(TelegramConfig(allow_from=["alice"]), MessageBus())

    assert channel.is_allowed("attacker|alice|extra") is False
    assert channel.is_allowed("not-a-number|alice") is False


# --- is_admin tests ---


def test_admin_users_defaults_to_empty_list() -> None:
    """admin_users defaults to empty list."""
    assert TelegramConfig().admin_users == []


def test_is_admin_empty_list_returns_false_for_all() -> None:
    """Empty admin_users list returns False for all users."""
    channel = TelegramChannel(TelegramConfig(admin_users=[]), MessageBus())

    assert channel.is_admin("12345") is False
    assert channel.is_admin("12345|alice") is False
    assert channel.is_admin("anyone") is False


def test_is_admin_listed_user_returns_true() -> None:
    """Listed user in admin_users returns True."""
    channel = TelegramChannel(TelegramConfig(admin_users=["12345", "alice"]), MessageBus())

    assert channel.is_admin("12345") is True
    assert channel.is_admin("12345|bob") is True  # ID matches
    assert channel.is_admin("99999|alice") is True  # username matches
    assert channel.is_admin("alice") is True  # direct match


def test_is_admin_unlisted_user_returns_false() -> None:
    """Unlisted user returns False."""
    channel = TelegramChannel(TelegramConfig(admin_users=["12345", "alice"]), MessageBus())

    assert channel.is_admin("99999") is False
    assert channel.is_admin("99999|bob") is False
    assert channel.is_admin("bob") is False


def test_is_admin_supports_full_sender_id_format() -> None:
    """admin_users can contain full id|username format."""
    channel = TelegramChannel(TelegramConfig(admin_users=["12345|alice"]), MessageBus())

    assert channel.is_admin("12345|alice") is True
    assert channel.is_admin("12345|bob") is False  # different username
    assert channel.is_admin("99999|alice") is False  # different id


@pytest.mark.asyncio
async def test_send_progress_keeps_message_in_topic() -> None:
    config = TelegramConfig(enabled=True, token="123:abc", allow_from=["*"])
    channel = TelegramChannel(config, MessageBus())
    channel._app = _FakeApp(lambda: None)

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="hello",
            metadata={"_progress": True, "message_thread_id": 42},
        )
    )

    assert channel._app.bot.sent_messages[0]["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_send_reply_infers_topic_from_message_id_cache() -> None:
    config = TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], reply_to_message=True)
    channel = TelegramChannel(config, MessageBus())
    channel._app = _FakeApp(lambda: None)
    channel._message_threads[("123", 10)] = 42

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="hello",
            metadata={"message_id": 10},
        )
    )

    assert channel._app.bot.sent_messages[0]["message_thread_id"] == 42
    assert channel._app.bot.sent_messages[0]["reply_parameters"].message_id == 10


@pytest.mark.asyncio
async def test_send_remote_media_url_after_security_validation(monkeypatch) -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    monkeypatch.setattr("nanobot.channels.telegram.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="",
            media=["https://example.com/cat.jpg"],
        )
    )

    assert channel._app.bot.sent_media == [
        {
            "kind": "photo",
            "chat_id": 123,
            "photo": "https://example.com/cat.jpg",
            "reply_parameters": None,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_preserves_filename(tmp_path: Path) -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    attachment = tmp_path / "report.final.md"
    attachment.write_bytes(b"# Report\n")

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="",
            media=[str(attachment)],
        )
    )

    assert channel._app.bot.sent_media == [
        {
            "kind": "document",
            "chat_id": 123,
            "document": b"# Report\n",
            "reply_parameters": None,
            "filename": "report.final.md",
        }
    ]


@pytest.mark.asyncio
async def test_send_blocks_unsafe_remote_media_url(monkeypatch) -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    monkeypatch.setattr(
        "nanobot.channels.telegram.validate_url_target",
        lambda url: (False, "Blocked: example.com resolves to private/internal address 127.0.0.1"),
    )

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="",
            media=["http://example.com/internal.jpg"],
        )
    )

    assert channel._app.bot.sent_media == []
    assert channel._app.bot.sent_messages == [
        {
            "chat_id": 123,
            "text": "[Failed to send: internal.jpg]",
            "reply_parameters": None,
        }
    ]


@pytest.mark.asyncio
async def test_group_policy_mention_ignores_unmentioned_group_message() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="mention"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    await channel._on_message(_make_telegram_update(text="hello everyone"), None)

    assert handled == []
    assert channel._app.bot.get_me_calls == 1


@pytest.mark.asyncio
async def test_group_policy_mention_accepts_text_mention_and_caches_bot_identity() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="mention"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    mention = SimpleNamespace(type="mention", offset=0, length=13)
    await channel._on_message(_make_telegram_update(text="@nanobot_test hi", entities=[mention]), None)
    await channel._on_message(_make_telegram_update(text="@nanobot_test again", entities=[mention]), None)

    assert len(handled) == 2
    assert channel._app.bot.get_me_calls == 1


@pytest.mark.asyncio
async def test_group_policy_mention_accepts_caption_mention() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="mention"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    mention = SimpleNamespace(type="mention", offset=0, length=13)
    await channel._on_message(
        _make_telegram_update(caption="@nanobot_test photo", caption_entities=[mention]),
        None,
    )

    assert len(handled) == 1
    assert handled[0]["content"] == "@nanobot_test photo"


@pytest.mark.asyncio
async def test_group_policy_mention_accepts_reply_to_bot() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="mention"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    reply = SimpleNamespace(from_user=SimpleNamespace(id=999))
    await channel._on_message(_make_telegram_update(text="reply", reply_to_message=reply), None)

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_policy_open_accepts_plain_group_message() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    await channel._on_message(_make_telegram_update(text="hello group"), None)

    assert len(handled) == 1
    assert channel._app.bot.get_me_calls == 0


@pytest.mark.asyncio
async def test_extract_reply_context_no_reply() -> None:
    """When there is no reply_to_message, _extract_reply_context returns None."""
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    message = SimpleNamespace(reply_to_message=None)
    assert await channel._extract_reply_context(message) is None


@pytest.mark.asyncio
async def test_extract_reply_context_with_text() -> None:
    """When reply has text, return prefixed string."""
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    channel._app = _FakeApp(lambda: None)
    reply = SimpleNamespace(text="Hello world", caption=None, from_user=SimpleNamespace(id=2, username="testuser", first_name="Test"))
    message = SimpleNamespace(reply_to_message=reply)
    assert await channel._extract_reply_context(message) == "[Reply to @testuser: Hello world]"


@pytest.mark.asyncio
async def test_extract_reply_context_with_caption_only() -> None:
    """When reply has only caption (no text), caption is used."""
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    channel._app = _FakeApp(lambda: None)
    reply = SimpleNamespace(text=None, caption="Photo caption", from_user=SimpleNamespace(id=2, username=None, first_name="Test"))
    message = SimpleNamespace(reply_to_message=reply)
    assert await channel._extract_reply_context(message) == "[Reply to Test: Photo caption]"


@pytest.mark.asyncio
async def test_extract_reply_context_truncation() -> None:
    """Reply text is truncated at TELEGRAM_REPLY_CONTEXT_MAX_LEN."""
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    channel._app = _FakeApp(lambda: None)
    long_text = "x" * (TELEGRAM_REPLY_CONTEXT_MAX_LEN + 100)
    reply = SimpleNamespace(text=long_text, caption=None, from_user=SimpleNamespace(id=2, username=None, first_name=None))
    message = SimpleNamespace(reply_to_message=reply)
    result = await channel._extract_reply_context(message)
    assert result is not None
    assert result.startswith("[Reply to: ")
    assert result.endswith("...]")
    assert len(result) == len("[Reply to: ]") + TELEGRAM_REPLY_CONTEXT_MAX_LEN + len("...")


@pytest.mark.asyncio
async def test_extract_reply_context_no_text_returns_none() -> None:
    """When reply has no text/caption, _extract_reply_context returns None (media handled separately)."""
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    reply = SimpleNamespace(text=None, caption=None)
    message = SimpleNamespace(reply_to_message=reply)
    assert await channel._extract_reply_context(message) is None


@pytest.mark.asyncio
async def test_on_message_includes_reply_context() -> None:
    """When user replies to a message, content passed to bus starts with reply context."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    reply = SimpleNamespace(text="Hello", message_id=2, from_user=SimpleNamespace(id=1))
    update = _make_telegram_update(text="translate this", reply_to_message=reply)
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert handled[0]["content"].startswith("[Reply to: Hello]")
    assert "translate this" in handled[0]["content"]


@pytest.mark.asyncio
async def test_download_message_media_returns_path_when_download_succeeds(
    monkeypatch, tmp_path
) -> None:
    """_download_message_media returns (paths, content_parts) when bot.get_file and download succeed."""
    media_dir = tmp_path / "media" / "telegram"
    media_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.channels.telegram.get_media_dir",
        lambda channel=None: media_dir if channel else tmp_path / "media",
    )

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(download_to_drive=AsyncMock(return_value=None))
    )

    msg = SimpleNamespace(
        photo=[SimpleNamespace(file_id="fid123", mime_type="image/jpeg")],
        voice=None,
        audio=None,
        document=None,
        video=None,
        video_note=None,
        animation=None,
    )
    paths, parts = await channel._download_message_media(msg)
    assert len(paths) == 1
    assert len(parts) == 1
    assert "fid123" in paths[0]
    assert "[image:" in parts[0]


@pytest.mark.asyncio
async def test_download_message_media_uses_file_unique_id_when_available(
    monkeypatch, tmp_path
) -> None:
    media_dir = tmp_path / "media" / "telegram"
    media_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.channels.telegram.get_media_dir",
        lambda channel=None: media_dir if channel else tmp_path / "media",
    )

    downloaded: dict[str, str] = {}

    async def _download_to_drive(path: str) -> None:
        downloaded["path"] = path

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    app = _FakeApp(lambda: None)
    app.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(download_to_drive=_download_to_drive)
    )
    channel._app = app

    msg = SimpleNamespace(
        photo=[
            SimpleNamespace(
                file_id="file-id-that-should-not-be-used",
                file_unique_id="stable-unique-id",
                mime_type="image/jpeg",
                file_name=None,
            )
        ],
        voice=None,
        audio=None,
        document=None,
        video=None,
        video_note=None,
        animation=None,
    )

    paths, parts = await channel._download_message_media(msg)

    assert downloaded["path"].endswith("stable-unique-id.jpg")
    assert paths == [str(media_dir / "stable-unique-id.jpg")]
    assert parts == [f"[image: {media_dir / 'stable-unique-id.jpg'}]"]


@pytest.mark.asyncio
async def test_on_message_attaches_reply_to_media_when_available(monkeypatch, tmp_path) -> None:
    """When user replies to a message with media, that media is downloaded and attached to the turn."""
    media_dir = tmp_path / "media" / "telegram"
    media_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.channels.telegram.get_media_dir",
        lambda channel=None: media_dir if channel else tmp_path / "media",
    )

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    app = _FakeApp(lambda: None)
    app.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(download_to_drive=AsyncMock(return_value=None))
    )
    channel._app = app
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    reply_with_photo = SimpleNamespace(
        text=None,
        caption=None,
        photo=[SimpleNamespace(file_id="reply_photo_fid", mime_type="image/jpeg")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
    )
    update = _make_telegram_update(
        text="what is the image?",
        reply_to_message=reply_with_photo,
    )
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert handled[0]["content"].startswith("[Reply to: [image:")
    assert "what is the image?" in handled[0]["content"]
    assert len(handled[0]["media"]) == 1
    assert "reply_photo_fid" in handled[0]["media"][0]


@pytest.mark.asyncio
async def test_on_message_reply_to_media_fallback_when_download_fails() -> None:
    """When reply has media but download fails, no media attached and no reply tag."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.get_file = None
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    reply_with_photo = SimpleNamespace(
        text=None,
        caption=None,
        photo=[SimpleNamespace(file_id="x", mime_type="image/jpeg")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
    )
    update = _make_telegram_update(text="what is this?", reply_to_message=reply_with_photo)
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert "what is this?" in handled[0]["content"]
    assert handled[0]["media"] == []


@pytest.mark.asyncio
async def test_on_message_reply_to_caption_and_media(monkeypatch, tmp_path) -> None:
    """When replying to a message with caption + photo, both text context and media are included."""
    media_dir = tmp_path / "media" / "telegram"
    media_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.channels.telegram.get_media_dir",
        lambda channel=None: media_dir if channel else tmp_path / "media",
    )

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    app = _FakeApp(lambda: None)
    app.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(download_to_drive=AsyncMock(return_value=None))
    )
    channel._app = app
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    reply_with_caption_and_photo = SimpleNamespace(
        text=None,
        caption="A cute cat",
        photo=[SimpleNamespace(file_id="cat_fid", mime_type="image/jpeg")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
    )
    update = _make_telegram_update(
        text="what breed is this?",
        reply_to_message=reply_with_caption_and_photo,
    )
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert "[Reply to: A cute cat]" in handled[0]["content"]
    assert "what breed is this?" in handled[0]["content"]
    assert len(handled[0]["media"]) == 1
    assert "cat_fid" in handled[0]["media"][0]


@pytest.mark.asyncio
async def test_forward_command_does_not_inject_reply_context() -> None:
    """Slash commands forwarded via _forward_command must not include reply context."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle

    reply = SimpleNamespace(text="some old message", message_id=2, from_user=SimpleNamespace(id=1))
    update = _make_telegram_update(text="/new", reply_to_message=reply)
    await channel._forward_command(update, None)

    assert len(handled) == 1
    assert handled[0]["content"] == "/new"


@pytest.mark.asyncio
async def test_forward_command_preserves_dream_log_args_and_strips_bot_suffix() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    update = _make_telegram_update(text="/dream-log@nanobot_test deadbeef", reply_to_message=None)

    await channel._forward_command(update, None)

    assert len(handled) == 1
    assert handled[0]["content"] == "/dream-log deadbeef"


@pytest.mark.asyncio
async def test_forward_command_normalizes_telegram_safe_dream_aliases() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    update = _make_telegram_update(text="/dream_restore@nanobot_test deadbeef", reply_to_message=None)

    await channel._forward_command(update, None)

    assert len(handled) == 1
    assert handled[0]["content"] == "/dream-restore deadbeef"


@pytest.mark.asyncio
async def test_on_help_includes_restart_command() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    update = _make_telegram_update(text="/help", chat_type="private")
    update.message.reply_text = AsyncMock()

    await channel._on_help(update, None)

    update.message.reply_text.assert_awaited_once()
    help_text = update.message.reply_text.await_args.args[0]
    assert "/restart" in help_text
    assert "/status" in help_text
    assert "/dream" in help_text
    assert "/dream-log" in help_text
    assert "/dream-restore" in help_text


@pytest.mark.asyncio
async def test_on_start_ignores_unauthorized_user_silently() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["999"], group_policy="open"),
        MessageBus(),
    )
    update = _make_telegram_update(text="/start", chat_type="private")
    update.message.reply_text = AsyncMock()

    await channel._on_start(update, None)

    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_help_ignores_unauthorized_user_silently() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["999"], group_policy="open"),
        MessageBus(),
    )
    update = _make_telegram_update(text="/help", chat_type="private")
    update.message.reply_text = AsyncMock()

    await channel._on_help(update, None)

    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_ignores_unauthorized_user_before_side_effects() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["999"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    started_typing: list[str] = []
    handled: list[dict] = []
    channel._start_typing = lambda chat_id: started_typing.append(chat_id)
    channel._add_reaction = AsyncMock(return_value=None)

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle

    await channel._on_message(_make_telegram_update(text="hello", chat_type="private"), None)

    assert started_typing == []
    channel._add_reaction.assert_not_awaited()
    assert handled == []


@pytest.mark.asyncio
async def test_on_message_location_content() -> None:
    """Location messages are forwarded as [location: lat, lon] content."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    location = SimpleNamespace(latitude=48.8566, longitude=2.3522)
    update = _make_telegram_update(location=location)
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert handled[0]["content"] == "[location: 48.8566, 2.3522]"


@pytest.mark.asyncio
async def test_on_message_location_with_text() -> None:
    """Location messages with accompanying text include both in content."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []
    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)
    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    location = SimpleNamespace(latitude=51.5074, longitude=-0.1278)
    update = _make_telegram_update(text="meet me here", location=location)
    await channel._on_message(update, None)

    assert len(handled) == 1
    assert "meet me here" in handled[0]["content"]
    assert "[location: 51.5074, -0.1278]" in handled[0]["content"]


# --- group_allow_from tests ---


def test_group_allow_from_defaults_to_empty_list() -> None:
    """group_allow_from defaults to empty list (allow all groups)."""
    assert TelegramConfig().group_allow_from == []


@pytest.mark.asyncio
async def test_group_allow_from_empty_allows_all_groups() -> None:
    """Empty group_allow_from list allows messages from any group (current behavior)."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open", group_allow_from=[]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Message from group -100123
    await channel._on_message(_make_telegram_update(text="hello"), None)

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_allow_from_filters_unlisted_groups() -> None:
    """Non-empty group_allow_from rejects messages from unlisted groups."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="open",
            group_allow_from=["-100999"],  # Only allow this group
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Message from group -100123 (not in allowlist)
    await channel._on_message(_make_telegram_update(text="hello"), None)

    assert handled == []


@pytest.mark.asyncio
async def test_group_allow_from_accepts_listed_groups() -> None:
    """Non-empty group_allow_from accepts messages from listed groups."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="open",
            group_allow_from=["-100123"],  # Allow this group (matches _make_telegram_update)
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Message from group -100123 (in allowlist)
    await channel._on_message(_make_telegram_update(text="hello"), None)

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_allow_from_does_not_affect_private_chats() -> None:
    """Private chats are unaffected by group_allow_from filter."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="open",
            group_allow_from=["-100999"],  # Only allow a different group
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Private chat message
    await channel._on_message(_make_telegram_update(text="hello", chat_type="private"), None)

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_allow_from_does_not_block_mentions() -> None:
    """@mentions in unlisted groups should still be accepted.

    group_allow_from should only filter non-mentioned messages when policy is open.
    Mentions should ALWAYS work, even from unlisted groups.
    """
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="mention",
            group_allow_from=["-100999"],  # Unlisted group (message from -100123)
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # @mention from unlisted group should be accepted
    mention = SimpleNamespace(type="mention", offset=0, length=13)
    await channel._on_message(
        _make_telegram_update(text="@nanobot_test hi", entities=[mention]),
        None,
    )

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_allow_from_does_not_block_replies_to_bot() -> None:
    """Replies to bot in unlisted groups should still be accepted."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="mention",
            group_allow_from=["-100999"],  # Unlisted group (message from -100123)
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Reply to bot from unlisted group should be accepted
    reply = SimpleNamespace(from_user=SimpleNamespace(id=999))  # bot_id from _FakeBot.get_me()
    await channel._on_message(
        _make_telegram_update(text="reply to bot", reply_to_message=reply),
        None,
    )

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_group_allow_from_filters_plain_messages_with_policy_open() -> None:
    """Plain messages from unlisted groups should be rejected when policy is open."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True,
            token="123:abc",
            allow_from=["*"],
            group_policy="open",
            group_allow_from=["-100999"],  # Unlisted group (message from -100123)
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    # Plain message from unlisted group should NOT be accepted
    await channel._on_message(_make_telegram_update(text="hello"), None)

    assert handled == []


# ---------------------------------------------------------------------------
# Tests for retry amplification fix (issue #3050)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_text_does_not_fallback_on_network_timeout() -> None:
    """TimedOut should propagate immediately, NOT trigger plain-text fallback.

    Before the fix, _send_text caught ALL exceptions (including TimedOut)
    and retried as plain text, doubling connection demand during pool
    exhaustion — see issue #3050.
    """
    from telegram.error import TimedOut

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    call_count = 0

    async def always_timeout(**kwargs):
        nonlocal call_count
        call_count += 1
        raise TimedOut()

    channel._app.bot.send_message = always_timeout

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        with pytest.raises(TimedOut):
            await channel._send_text(123, "hello", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    # With the fix: only _call_with_retry's 3 HTML attempts (no plain fallback).
    # Before the fix: 3 HTML + 3 plain = 6 attempts.
    assert call_count == 3, (
        f"Expected 3 calls (HTML retries only), got {call_count} "
        "(plain-text fallback should not trigger on TimedOut)"
    )


@pytest.mark.asyncio
async def test_send_text_does_not_fallback_on_network_error() -> None:
    """NetworkError should propagate immediately, NOT trigger plain-text fallback."""
    from telegram.error import NetworkError

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    call_count = 0

    async def always_network_error(**kwargs):
        nonlocal call_count
        call_count += 1
        raise NetworkError("Connection reset")

    channel._app.bot.send_message = always_network_error

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        with pytest.raises(NetworkError):
            await channel._send_text(123, "hello", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    # _call_with_retry does NOT retry NetworkError (only TimedOut/RetryAfter),
    # so it raises after 1 attempt. The fix prevents plain-text fallback.
    # Before the fix: 1 HTML + 1 plain = 2. After the fix: 1 HTML only.
    assert call_count == 1, (
        f"Expected 1 call (HTML only, no plain fallback), got {call_count}"
    )


@pytest.mark.asyncio
async def test_send_text_falls_back_on_bad_request() -> None:
    """BadRequest (HTML parse error) should still trigger plain-text fallback."""
    from telegram.error import BadRequest

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    original_send = channel._app.bot.send_message
    html_call_count = 0

    async def html_fails(**kwargs):
        nonlocal html_call_count
        if kwargs.get("parse_mode") == "HTML":
            html_call_count += 1
            raise BadRequest("Can't parse entities")
        return await original_send(**kwargs)

    channel._app.bot.send_message = html_fails

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        await channel._send_text(123, "hello **world**", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    # HTML attempt failed with BadRequest → fallback to plain text succeeds.
    assert html_call_count == 1, f"Expected 1 HTML attempt, got {html_call_count}"
    assert len(channel._app.bot.sent_messages) == 1
    # Plain text send should NOT have parse_mode
    assert channel._app.bot.sent_messages[0].get("parse_mode") is None


@pytest.mark.asyncio
async def test_send_text_bad_request_plain_fallback_exhausted() -> None:
    """When both HTML and plain-text fallback fail with BadRequest, the error propagates."""
    from telegram.error import BadRequest

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    call_count = 0

    async def always_bad_request(**kwargs):
        nonlocal call_count
        call_count += 1
        raise BadRequest("Bad request")

    channel._app.bot.send_message = always_bad_request

    import nanobot.channels.telegram as tg_mod
    orig_delay = tg_mod._SEND_RETRY_BASE_DELAY
    tg_mod._SEND_RETRY_BASE_DELAY = 0.01
    try:
        with pytest.raises(BadRequest):
            await channel._send_text(123, "hello", None, {})
    finally:
        tg_mod._SEND_RETRY_BASE_DELAY = orig_delay

    # _call_with_retry does NOT retry BadRequest (only TimedOut/RetryAfter),
    # so HTML fails after 1 attempt → fallback to plain also fails after 1 attempt.
    # Before the fix: 2 total. After the fix: still 2 (BadRequest SHOULD fallback).
    assert call_count == 2, f"Expected 2 calls (1 HTML + 1 plain), got {call_count}"


# ---------------------------------------------------------------------------
# _markdown_to_telegram_html formatting tests
# ---------------------------------------------------------------------------

def test_markdown_to_html_headers_become_bold() -> None:
    from nanobot.channels.telegram import _markdown_to_telegram_html

    assert _markdown_to_telegram_html("# Title") == "<b>Title</b>"
    assert _markdown_to_telegram_html("## Subtitle") == "<b>Subtitle</b>"
    assert _markdown_to_telegram_html("### Deep") == "<b>Deep</b>"


def test_markdown_to_html_numbered_lists_preserved() -> None:
    from nanobot.channels.telegram import _markdown_to_telegram_html

    text = "1. First\n2. Second\n3. Third"
    result = _markdown_to_telegram_html(text)
    assert "1. First" in result
    assert "2. Second" in result
    assert "3. Third" in result


def test_markdown_to_html_numbered_list_normalizes_whitespace() -> None:
    from nanobot.channels.telegram import _markdown_to_telegram_html

    # Extra spaces after dot should be normalized
    text = "1.   Lots of space\n2.  Two spaces"
    result = _markdown_to_telegram_html(text)
    assert "1. Lots of space" in result
    assert "2. Two spaces" in result


def test_markdown_to_html_headers_survive_html_escaping() -> None:
    """Headers containing special HTML chars should still render as bold."""
    from nanobot.channels.telegram import _markdown_to_telegram_html

    result = _markdown_to_telegram_html("# A < B & C > D")
    assert "<b>A &lt; B &amp; C &gt; D</b>" == result


def test_markdown_to_html_mixed_formatting() -> None:
    """Headers, bullets, numbered lists, and bold coexist correctly."""
    from nanobot.channels.telegram import _markdown_to_telegram_html

    text = "# Overview\n\n- bullet one\n- bullet two\n\n1. step one\n2. step two\n\n**bold text**"
    result = _markdown_to_telegram_html(text)
    assert "<b>Overview</b>" in result
    assert "\u2022 bullet one" in result
    assert "1. step one" in result
    assert "<b>bold text</b>" in result


# ---------------------------------------------------------------------------
# _strip_md_block tests
# ---------------------------------------------------------------------------

def test_strip_md_block_removes_inline_formatting() -> None:
    from nanobot.channels.telegram import _strip_md_block

    text = "**bold** and _italic_ and ~~struck~~"
    result = _strip_md_block(text)
    assert result == "bold and italic and struck"


def test_strip_md_block_strips_headers() -> None:
    from nanobot.channels.telegram import _strip_md_block

    assert _strip_md_block("## Title\nBody") == "Title\nBody"


def test_strip_md_block_converts_bullets_and_numbers() -> None:
    from nanobot.channels.telegram import _strip_md_block

    text = "- item a\n1. item b\n2. item c"
    result = _strip_md_block(text)
    assert "\u2022 item a" in result
    assert "1. item b" in result
    assert "2. item c" in result


def test_strip_md_block_strips_links() -> None:
    from nanobot.channels.telegram import _strip_md_block

    assert _strip_md_block("[click here](https://example.com)") == "click here"


# ---------------------------------------------------------------------------
# Streaming mid-edit uses _strip_md_block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_delta_mid_stream_strips_markdown() -> None:
    """Mid-stream edits should strip markdown so users see clean text."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._app.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    channel._app.bot.edit_message_text = AsyncMock()

    # Initial send with markdown
    await channel.send_delta("999", "**hello** world")
    sent_text = channel._app.bot.send_message.call_args.kwargs.get("text", "")
    # Should NOT contain raw markdown asterisks
    assert "**" not in sent_text
    assert "hello world" in sent_text

    # Mid-stream edit
    import time
    buf = channel._stream_bufs["999"]
    buf.last_edit = time.monotonic() - 10  # force edit interval
    await channel.send_delta("999", "\n### Title\n1. step")
    edited_text = channel._app.bot.edit_message_text.call_args.kwargs.get("text", "")
    assert "###" not in edited_text
    assert "**" not in edited_text
    assert "Title" in edited_text
    assert "1. step" in edited_text


def test_build_keyboard_respects_inline_keyboards_flag() -> None:
    """``_build_keyboard`` returns ``None`` whenever the feature flag is off,
    regardless of whether buttons are provided; returns a proper Markup only
    when the flag is explicitly enabled. Pins the kill-switch so accidentally
    flipping the default doesn't silently expose callback handlers."""
    from telegram import InlineKeyboardMarkup

    off = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", inline_keyboards=False),
        MessageBus(),
    )
    assert off._build_keyboard([["A", "B"]]) is None

    on = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", inline_keyboards=True),
        MessageBus(),
    )
    assert on._build_keyboard([]) is None  # empty still no-op
    markup = on._build_keyboard([["Yes", "No"], ["Cancel"]])
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = markup.inline_keyboard
    assert [[b.text for b in row] for row in rows] == [["Yes", "No"], ["Cancel"]]
    # callback_data mirrors label so _on_callback_query can echo the tap back.
    assert rows[0][0].callback_data == "Yes"


def test_safe_callback_data_truncates_at_utf8_boundary() -> None:
    # Telegram's 64-byte callback_data cap is a hard API limit; silent 400s were the bug.
    short = "Yes"
    assert TelegramChannel._safe_callback_data(short) == short

    long_ascii = "a" * 100
    out = TelegramChannel._safe_callback_data(long_ascii)
    assert len(out.encode("utf-8")) <= 64
    assert long_ascii.startswith(out)

    # Multibyte labels must not split a codepoint mid-byte.
    long_cjk = "同意并继续下一步，我已阅读并同意了服务条款以及隐私政策"
    assert len(long_cjk.encode("utf-8")) > 64
    out = TelegramChannel._safe_callback_data(long_cjk)
    assert len(out.encode("utf-8")) <= 64
    assert long_cjk.startswith(out)
    out.encode("utf-8").decode("utf-8")  # must round-trip cleanly


def test_build_keyboard_uses_safe_callback_data_for_long_labels() -> None:
    # Pins the integration so a long-label payload survives ``send_message`` instead of 400ing.
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", inline_keyboards=True),
        MessageBus(),
    )
    long_label = "Approve and continue to the next step with the updated terms of service"
    assert len(long_label.encode("utf-8")) > 64

    markup = channel._build_keyboard([[long_label]])
    btn = markup.inline_keyboard[0][0]
    assert btn.text == long_label  # display preserved
    assert len(btn.callback_data.encode("utf-8")) <= 64
    assert long_label.startswith(btn.callback_data)


def test_buttons_as_text_format_preserves_rows_and_labels() -> None:
    # Canonical shape: one row per line, labels bracketed. Layout survives the fallback.
    assert TelegramChannel._buttons_as_text([["Yes", "No"], ["Cancel"]]) == "[Yes] [No]\n[Cancel]"
    assert TelegramChannel._buttons_as_text([["Only"]]) == "[Only]"
    assert TelegramChannel._buttons_as_text([[], ["A"]]) == "[A]"  # empty rows skipped


@pytest.mark.asyncio
async def test_send_falls_back_buttons_to_inline_text_when_flag_off() -> None:
    """Buttons are semantic options; with ``inline_keyboards=False`` we must
    splice labels into the text so users still see the choices. Silent-drop
    was the pre-fallback bug — the agent got a success reply while the user
    saw a question with no options."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], inline_keyboards=False),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="Proceed?",
            buttons=[["Yes", "No"], ["Cancel"]],
        )
    )

    assert len(channel._app.bot.sent_messages) == 1
    sent = channel._app.bot.sent_messages[0]
    assert sent.get("reply_markup") is None
    assert "Proceed?" in sent["text"]
    assert "[Yes] [No]" in sent["text"]
    assert "[Cancel]" in sent["text"]


@pytest.mark.asyncio
async def test_send_uses_native_keyboard_when_flag_on() -> None:
    """With the flag on, the content stays clean and buttons ride in ``reply_markup``."""
    from telegram import InlineKeyboardMarkup

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], inline_keyboards=True),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="Proceed?",
            buttons=[["Yes", "No"]],
        )
    )

    sent = channel._app.bot.sent_messages[0]
    assert isinstance(sent.get("reply_markup"), InlineKeyboardMarkup)
    assert "[Yes]" not in sent["text"]  # native keyboard owns the rendering


@pytest.mark.asyncio
async def test_callback_query_ignores_unauthorized_user_before_side_effects() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["999"], inline_keyboards=True),
        MessageBus(),
    )
    channel._handle_message = AsyncMock()

    query = SimpleNamespace(
        id="cb_1",
        data="Yes",
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat_id=123,
            edit_reply_markup=AsyncMock(),
        ),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=12345, username="alice", first_name="Alice"),
    )

    await channel._on_callback_query(update, None)

    query.answer.assert_not_awaited()
    query.message.edit_reply_markup.assert_not_awaited()
    channel._handle_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# /addgroup, /removegroup, /groups command tests
# ---------------------------------------------------------------------------


@pytest.fixture
def groups_file(tmp_path, monkeypatch):
    """Fixture to isolate telegram_groups.json to a temp directory."""
    groups_path = tmp_path / "telegram_groups.json"
    monkeypatch.setattr(
        "nanobot.channels.telegram._get_groups_file",
        lambda: groups_path,
    )
    return groups_path


def _make_admin_update(text: str, user_id: int = 12345, username: str = "alice"):
    """Helper to create updates for admin command tests."""
    user = SimpleNamespace(id=user_id, username=username, first_name="Alice")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        chat_id=user_id,
        text=text,
        message_id=1,
        message_thread_id=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_user=user)


def test_load_groups_data_returns_empty_when_file_missing(groups_file):
    """_load_groups_data returns empty structure when file doesn't exist."""
    assert not groups_file.exists()
    data = _load_groups_data()
    assert data == {"allowed": [], "seen": []}


def test_load_groups_data_returns_saved_data(groups_file):
    """_load_groups_data returns saved data."""
    test_data = {
        "allowed": [{"id": "-123", "name": "Test Group", "added": "2026-05-14"}],
        "seen": [{"id": "-456", "name": "Seen Group", "last_seen": "2026-05-14T10:30:00"}],
    }
    groups_file.write_text(json.dumps(test_data))
    data = _load_groups_data()
    assert data == test_data


def test_save_groups_data_creates_file(groups_file):
    """_save_groups_data creates the file."""
    test_data = {"allowed": [], "seen": [{"id": "-123", "name": "Test", "last_seen": "2026-05-14T10:30:00"}]}
    _save_groups_data(test_data)
    assert groups_file.exists()
    loaded = json.loads(groups_file.read_text())
    assert loaded == test_data


def test_prune_old_seen_groups_removes_expired_entries():
    """_prune_old_seen_groups removes entries older than 30 days."""
    old_time = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    recent_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data = {
        "allowed": [],
        "seen": [
            {"id": "-111", "name": "Old Group", "last_seen": old_time},
            {"id": "-222", "name": "Recent Group", "last_seen": recent_time},
        ],
    }
    pruned = _prune_old_seen_groups(data)
    assert pruned is True
    assert len(data["seen"]) == 1
    assert data["seen"][0]["id"] == "-222"


def test_prune_old_seen_groups_keeps_recent_entries():
    """_prune_old_seen_groups keeps entries within 30 days."""
    recent_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    data = {
        "allowed": [],
        "seen": [{"id": "-111", "name": "Recent Group", "last_seen": recent_time}],
    }
    pruned = _prune_old_seen_groups(data)
    assert pruned is False
    assert len(data["seen"]) == 1


def test_format_relative_time_hours():
    """_format_relative_time formats hours correctly."""
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    result = _format_relative_time(two_hours_ago)
    assert result == "2h ago"


def test_format_relative_time_days():
    """_format_relative_time formats days correctly."""
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    result = _format_relative_time(three_days_ago)
    assert result == "3d ago"


def test_format_relative_time_minutes():
    """_format_relative_time formats minutes correctly."""
    fifteen_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    result = _format_relative_time(fifteen_minutes_ago)
    assert result == "15m ago"


def test_track_seen_group_adds_new_group(groups_file):
    """_track_seen_group adds a new group to seen list."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._track_seen_group(-100123456, "Test Group")

    data = _load_groups_data()
    assert len(data["seen"]) == 1
    assert data["seen"][0]["id"] == "-100123456"
    assert data["seen"][0]["name"] == "Test Group"
    assert "last_seen" in data["seen"][0]


def test_track_seen_group_updates_existing_group(groups_file):
    """_track_seen_group updates existing group's last_seen."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    initial_data = {
        "allowed": [],
        "seen": [{"id": "-100123456", "name": "Old Name", "last_seen": old_time}],
    }
    _save_groups_data(initial_data)

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._track_seen_group(-100123456, "New Name")

    data = _load_groups_data()
    assert len(data["seen"]) == 1
    assert data["seen"][0]["name"] == "New Name"
    assert data["seen"][0]["last_seen"] != old_time


def test_track_seen_group_skips_allowed_groups(groups_file):
    """_track_seen_group does not track groups in the allowed list."""
    initial_data = {
        "allowed": [{"id": "-100123456", "name": "Allowed Group", "added": "2026-05-14"}],
        "seen": [],
    }
    _save_groups_data(initial_data)

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    channel._track_seen_group(-100123456, "Allowed Group")

    data = _load_groups_data()
    assert len(data["seen"]) == 0


@pytest.mark.asyncio
async def test_addgroup_requires_admin() -> None:
    """Non-admins are rejected by /addgroup."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["999"]),
        MessageBus(),
    )

    update = _make_admin_update("/addgroup -100123")
    await channel._on_addgroup(update, None)

    update.message.reply_text.assert_awaited_once()
    assert "admin-only" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_addgroup_validates_group_id() -> None:
    """Invalid group IDs are rejected."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )

    update = _make_admin_update("/addgroup notanumber")
    await channel._on_addgroup(update, None)

    update.message.reply_text.assert_awaited_once()
    assert "Usage:" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_addgroup_adds_to_runtime_and_persists(groups_file) -> None:
    """Admin can add a group which updates runtime and persists."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )

    update = _make_admin_update("/addgroup -100999")
    await channel._on_addgroup(update, None)

    assert "-100999" in channel._runtime_groups
    assert groups_file.exists()
    data = json.loads(groups_file.read_text())
    assert "-100999" in data["allowed"]
    assert "Added group" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_addgroup_rejects_duplicate() -> None:
    """Adding an already-allowed group shows a message."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True, token="123:abc", allow_from=["*"],
            admin_users=["12345"], group_allow_from=["-100123"]
        ),
        MessageBus(),
    )

    update = _make_admin_update("/addgroup -100123")
    await channel._on_addgroup(update, None)

    assert "already in the allowlist" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_removegroup_requires_admin() -> None:
    """Non-admins are rejected by /removegroup."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["999"]),
        MessageBus(),
    )

    update = _make_admin_update("/removegroup -100123")
    await channel._on_removegroup(update, None)

    update.message.reply_text.assert_awaited_once()
    assert "admin-only" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_removegroup_removes_persisted_group(groups_file) -> None:
    """Admin can remove a persisted group."""
    groups_file.write_text('{"allowed": ["-100999"]}')

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._load_and_merge_groups()

    update = _make_admin_update("/removegroup -100999")
    await channel._on_removegroup(update, None)

    assert "-100999" not in channel._runtime_groups
    data = json.loads(groups_file.read_text())
    assert "-100999" not in data["allowed"]
    assert "Removed group" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_removegroup_rejects_config_groups() -> None:
    """Groups defined in config cannot be removed dynamically."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True, token="123:abc", allow_from=["*"],
            admin_users=["12345"], group_allow_from=["-100123"]
        ),
        MessageBus(),
    )

    update = _make_admin_update("/removegroup -100123")
    await channel._on_removegroup(update, None)

    assert "defined in config" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_removegroup_rejects_nonexistent_group() -> None:
    """Removing a group not in the allowlist shows a message."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )

    update = _make_admin_update("/removegroup -100999")
    await channel._on_removegroup(update, None)

    assert "not in the allowlist" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_groups_requires_admin() -> None:
    """Non-admins are rejected by /groups."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["999"]),
        MessageBus(),
    )

    update = _make_admin_update("/groups")
    await channel._on_groups(update, None)

    update.message.reply_text.assert_awaited_once()
    assert "admin-only" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_groups_lists_groups_with_source(groups_file) -> None:
    """Admin can list groups and see their source (config vs persisted)."""
    groups_file.write_text('{"allowed": ["-100999"]}')

    channel = TelegramChannel(
        TelegramConfig(
            enabled=True, token="123:abc", allow_from=["*"],
            admin_users=["12345"], group_allow_from=["-100123"]
        ),
        MessageBus(),
    )
    channel._load_and_merge_groups()

    update = _make_admin_update("/groups")
    await channel._on_groups(update, None)

    response = update.message.reply_text.await_args.args[0]
    assert "-100123" in response
    assert "(config)" in response
    assert "-100999" in response
    assert "(persisted)" in response


@pytest.mark.asyncio
async def test_groups_shows_empty_message_when_no_groups() -> None:
    """When no groups are in the allowlist, a helpful message is shown."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )

    update = _make_admin_update("/groups")
    await channel._on_groups(update, None)

    response = update.message.reply_text.await_args.args[0]
    assert "Allowed groups: (none)" in response


@pytest.mark.asyncio
async def test_on_groups_shows_seen_groups_with_timestamp(groups_file):
    """Seen groups are displayed with relative timestamp."""
    recent_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    initial_data = {
        "allowed": [],
        "seen": [{"id": "-100987654321", "name": "Random Chat", "last_seen": recent_time}],
    }
    _save_groups_data(initial_data)

    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )

    update = _make_admin_update("/groups")
    await channel._on_groups(update, None)

    reply_text = update.message.reply_text.await_args.args[0]
    assert "-100987654321" in reply_text
    assert "Random Chat" in reply_text
    assert "seen 2h ago" in reply_text


@pytest.mark.asyncio
async def test_addgroup_survives_restart(groups_file) -> None:
    """Groups added via /addgroup persist across channel restarts."""
    # First channel instance: add a group
    channel1 = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    update = _make_admin_update("/addgroup -100999")
    await channel1._on_addgroup(update, None)
    assert "-100999" in channel1._runtime_groups

    # Second channel instance: group should be loaded from file
    channel2 = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel2._load_and_merge_groups()
    assert "-100999" in channel2._runtime_groups


@pytest.mark.asyncio
async def test_on_message_tracks_seen_group(groups_file):
    """_on_message tracks seen groups when messages come from group chats."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)
    channel._handle_message = AsyncMock()
    channel._start_typing = lambda _: None

    async def mock_get_member_count():
        return 2

    user = SimpleNamespace(id=12345, username="alice", first_name="Alice")
    message = SimpleNamespace(
        chat=SimpleNamespace(
            type="group",
            is_forum=False,
            title="Test Group Chat",
            id=-100999888,
            get_member_count=mock_get_member_count,
        ),
        chat_id=-100999888,
        text="hello",
        caption=None,
        entities=[],
        caption_entities=[],
        reply_to_message=None,
        photo=None,
        voice=None,
        audio=None,
        document=None,
        video=None,
        video_note=None,
        animation=None,
        location=None,
        media_group_id=None,
        message_thread_id=None,
        message_id=1,
    )
    update = SimpleNamespace(message=message, effective_user=user)

    await channel._on_message(update, None)

    data = _load_groups_data()
    assert len(data["seen"]) == 1
    assert data["seen"][0]["id"] == "-100999888"
    assert data["seen"][0]["name"] == "Test Group Chat"


# --- _on_my_chat_member tests (bot joins group) ---


def _make_my_chat_member_update(
    chat_id: int,
    chat_title: str,
    old_status: str,
    new_status: str,
):
    """Helper to create my_chat_member updates for join/leave tests."""
    old_member = SimpleNamespace(status=old_status)
    new_member = SimpleNamespace(status=new_status)
    chat = SimpleNamespace(id=chat_id, type="supergroup", title=chat_title)
    chat_member = SimpleNamespace(
        chat=chat,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )
    return SimpleNamespace(my_chat_member=chat_member)


@pytest.mark.asyncio
async def test_bot_join_notifies_admin(groups_file) -> None:
    """When bot joins a group, admins receive a DM notification."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_my_chat_member_update(
        chat_id=-5275099223,
        chat_title="Cancun Trip",
        old_status="left",
        new_status="member",
    )

    await channel._on_my_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 1
    msg = channel._app.bot.sent_messages[0]
    assert msg["chat_id"] == 12345
    assert "Added to group 'Cancun Trip' (ID: -5275099223)" in msg["text"]
    assert "Use /addgroup -5275099223 to enable responses." in msg["text"]


@pytest.mark.asyncio
async def test_bot_join_notifies_multiple_admins(groups_file) -> None:
    """When bot joins a group, all configured admins receive a DM."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True, token="123:abc", allow_from=["*"],
            admin_users=["12345", "67890", "11111"],
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_my_chat_member_update(
        chat_id=-100123456,
        chat_title="Test Group",
        old_status="left",
        new_status="member",
    )

    await channel._on_my_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 3
    notified_admins = {msg["chat_id"] for msg in channel._app.bot.sent_messages}
    assert notified_admins == {12345, 67890, 11111}


@pytest.mark.asyncio
async def test_bot_join_adds_to_seen_list(groups_file) -> None:
    """When bot joins a group, the group is added to the seen list."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_my_chat_member_update(
        chat_id=-100999888,
        chat_title="New Group",
        old_status="left",
        new_status="member",
    )

    await channel._on_my_chat_member(update, None)

    data = _load_groups_data()
    assert len(data["seen"]) == 1
    assert data["seen"][0]["id"] == "-100999888"
    assert data["seen"][0]["name"] == "New Group"


@pytest.mark.asyncio
async def test_bot_join_no_notification_without_admins(groups_file) -> None:
    """No notification sent if admin_users is empty."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=[]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_my_chat_member_update(
        chat_id=-100123456,
        chat_title="Test Group",
        old_status="left",
        new_status="member",
    )

    await channel._on_my_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0


@pytest.mark.asyncio
async def test_bot_leave_does_not_notify(groups_file) -> None:
    """Bot leaving a group does not trigger admin notification."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_my_chat_member_update(
        chat_id=-100123456,
        chat_title="Test Group",
        old_status="member",
        new_status="left",
    )

    await channel._on_my_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0


@pytest.mark.asyncio
async def test_bot_join_private_chat_ignored(groups_file) -> None:
    """Bot being added to private chat does not trigger notification."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    old_member = SimpleNamespace(status="left")
    new_member = SimpleNamespace(status="member")
    chat = SimpleNamespace(id=99999, type="private", title=None)
    chat_member = SimpleNamespace(
        chat=chat,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )
    update = SimpleNamespace(my_chat_member=chat_member)

    await channel._on_my_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0


# --- _on_chat_member tests (other user joins group - privacy boundary) ---


def _make_chat_member_update(
    chat_id: int,
    chat_title: str,
    old_status: str,
    new_status: str,
    member_count: int = 3,
):
    """Helper to create chat_member updates for member join/leave tests.

    The chat.get_member_count() is mocked to return the specified member_count.
    """
    old_member = SimpleNamespace(status=old_status)
    new_member = SimpleNamespace(status=new_status)

    async def mock_get_member_count():
        return member_count

    chat = SimpleNamespace(
        id=chat_id,
        type="supergroup",
        title=chat_title,
        get_member_count=mock_get_member_count,
    )
    chat_member = SimpleNamespace(
        chat=chat,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )
    return SimpleNamespace(chat_member=chat_member)


@pytest.mark.asyncio
async def test_privacy_boundary_notifies_admin_when_group_grows_to_3_members(groups_file) -> None:
    """When a group goes from 2 to 3 members, admins receive a DM about privacy."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_chat_member_update(
        chat_id=-100123456,
        chat_title="Family Trip",
        old_status="left",
        new_status="member",
        member_count=3,  # Group just grew to 3 members
    )

    await channel._on_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 1
    msg = channel._app.bot.sent_messages[0]
    assert msg["chat_id"] == 12345
    assert "Family Trip" in msg["text"]
    assert "multiple members" in msg["text"]
    assert "private info" in msg["text"]


@pytest.mark.asyncio
async def test_privacy_boundary_no_notification_when_group_already_has_many_members(
    groups_file,
) -> None:
    """No notification when a member joins an already multi-user group."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_chat_member_update(
        chat_id=-100123456,
        chat_title="Large Group",
        old_status="left",
        new_status="member",
        member_count=10,  # Group already has many members
    )

    await channel._on_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0


@pytest.mark.asyncio
async def test_privacy_boundary_no_notification_on_member_leave(groups_file) -> None:
    """No notification when a member leaves (only on joins)."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_chat_member_update(
        chat_id=-100123456,
        chat_title="Group",
        old_status="member",
        new_status="left",
        member_count=2,
    )

    await channel._on_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0


@pytest.mark.asyncio
async def test_privacy_boundary_notifies_multiple_admins(groups_file) -> None:
    """All configured admins receive the privacy boundary notification."""
    channel = TelegramChannel(
        TelegramConfig(
            enabled=True, token="123:abc", allow_from=["*"],
            admin_users=["12345", "67890"],
        ),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    update = _make_chat_member_update(
        chat_id=-100123456,
        chat_title="Growing Group",
        old_status="left",
        new_status="member",
        member_count=3,
    )

    await channel._on_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 2
    notified_admins = {msg["chat_id"] for msg in channel._app.bot.sent_messages}
    assert notified_admins == {12345, 67890}


@pytest.mark.asyncio
async def test_privacy_boundary_ignored_for_private_chats(groups_file) -> None:
    """No notification when chat_member update is for a private chat."""
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], admin_users=["12345"]),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    async def mock_get_member_count():
        return 3

    chat = SimpleNamespace(
        id=99999,
        type="private",
        title=None,
        get_member_count=mock_get_member_count,
    )
    old_member = SimpleNamespace(status="left")
    new_member = SimpleNamespace(status="member")
    chat_member = SimpleNamespace(
        chat=chat,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )
    update = SimpleNamespace(chat_member=chat_member)

    await channel._on_chat_member(update, None)

    assert len(channel._app.bot.sent_messages) == 0
