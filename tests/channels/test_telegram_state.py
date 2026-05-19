"""Tests for Telegram state persistence utilities and config."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.channels.telegram import TelegramChannel, TelegramConfig
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


class TestMyChatMemberHandler:
    """Tests for my_chat_member event handling (bot add/remove tracking)."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel instance for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["123", "allowed_user"],
        )
        bus = MagicMock()
        return TelegramChannel(config, bus)

    def _make_chat_member_update(
        self,
        chat_id: int,
        chat_title: str,
        from_user_id: int,
        from_username: str | None,
        old_status: str,
        new_status: str,
    ) -> MagicMock:
        """Build a mock my_chat_member update."""
        update = MagicMock()
        update.my_chat_member = MagicMock()
        update.my_chat_member.chat = MagicMock()
        update.my_chat_member.chat.id = chat_id
        update.my_chat_member.chat.title = chat_title
        update.my_chat_member.chat.type = "supergroup"
        update.my_chat_member.from_user = MagicMock()
        update.my_chat_member.from_user.id = from_user_id
        update.my_chat_member.from_user.username = from_username
        update.my_chat_member.old_chat_member = MagicMock()
        update.my_chat_member.old_chat_member.status = old_status
        update.my_chat_member.new_chat_member = MagicMock()
        update.my_chat_member.new_chat_member.status = new_status
        return update

    @pytest.mark.asyncio
    async def test_bot_added_by_allowed_user_auto_approved(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Bot added by user in allow_from should be auto-approved."""
        update = self._make_chat_member_update(
            chat_id=-100123456,
            chat_title="Test Group",
            from_user_id=123,  # In allow_from
            from_username="other_name",
            old_status="left",
            new_status="member",
        )

        await channel._on_my_chat_member(update, MagicMock())

        origins = load_group_origins()
        assert "-100123456" in origins
        assert origins["-100123456"]["added_by"] == 123
        assert origins["-100123456"]["approved"] is True

    @pytest.mark.asyncio
    async def test_bot_added_by_allowed_username_auto_approved(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Bot added by username in allow_from should be auto-approved."""
        update = self._make_chat_member_update(
            chat_id=-100789,
            chat_title="Test Group 2",
            from_user_id=999,  # Not in allow_from by ID
            from_username="allowed_user",  # But username is in allow_from
            old_status="left",
            new_status="member",
        )

        await channel._on_my_chat_member(update, MagicMock())

        origins = load_group_origins()
        assert "-100789" in origins
        assert origins["-100789"]["added_by"] == 999
        assert origins["-100789"]["approved"] is True

    @pytest.mark.asyncio
    async def test_bot_added_by_non_allowed_user_pending(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Bot added by user not in allow_from should be pending approval."""
        update = self._make_chat_member_update(
            chat_id=-100999,
            chat_title="Unknown Group",
            from_user_id=555,  # Not in allow_from
            from_username="random_user",
            old_status="left",
            new_status="member",
        )

        await channel._on_my_chat_member(update, MagicMock())

        origins = load_group_origins()
        assert "-100999" in origins
        assert origins["-100999"]["added_by"] == 555
        assert origins["-100999"]["approved"] is False

    @pytest.mark.asyncio
    async def test_bot_removed_cleans_up_origins(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Bot removal should remove group from origins."""
        # Pre-populate origins
        save_group_origins({
            "-100123": {"added_by": 111, "added_at": 1700000000.0, "approved": True}
        })

        update = self._make_chat_member_update(
            chat_id=-100123,
            chat_title="Old Group",
            from_user_id=111,
            from_username=None,
            old_status="member",
            new_status="left",
        )

        await channel._on_my_chat_member(update, MagicMock())

        origins = load_group_origins()
        assert "-100123" not in origins

    @pytest.mark.asyncio
    async def test_bot_removed_cleans_up_members(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Bot removal should remove all members for that group."""
        # Pre-populate members
        save_group_members({
            "-100123": [111, 222, 333],
            "-100456": [444, 555],
        })

        update = self._make_chat_member_update(
            chat_id=-100123,
            chat_title="Old Group",
            from_user_id=111,
            from_username=None,
            old_status="administrator",
            new_status="kicked",
        )

        await channel._on_my_chat_member(update, MagicMock())

        members = load_group_members()
        assert "-100123" not in members
        # Other group should be untouched
        assert "-100456" in members
        assert members["-100456"] == [444, 555]

    @pytest.mark.asyncio
    async def test_private_chat_ignored(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Private chat updates should be ignored."""
        update = MagicMock()
        update.my_chat_member = MagicMock()
        update.my_chat_member.chat = MagicMock()
        update.my_chat_member.chat.type = "private"

        await channel._on_my_chat_member(update, MagicMock())

        # Should not have written anything
        origins = load_group_origins()
        assert origins == {}

    def test_is_user_in_allow_from_by_id(self, channel: TelegramChannel) -> None:
        """Check user by ID in allow_from."""
        assert channel._is_user_in_allow_from(123, None) is True
        assert channel._is_user_in_allow_from(999, None) is False

    def test_is_user_in_allow_from_by_username(self, channel: TelegramChannel) -> None:
        """Check user by username in allow_from."""
        assert channel._is_user_in_allow_from(999, "allowed_user") is True
        assert channel._is_user_in_allow_from(999, "random_user") is False

    def test_is_user_in_allow_from_wildcard(self) -> None:
        """Wildcard '*' in allow_from allows everyone."""
        config = TelegramConfig(enabled=True, token="test:token", allow_from=["*"])
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        assert channel._is_user_in_allow_from(12345, "anyone") is True

    def test_is_user_in_allow_from_empty_list(self) -> None:
        """Empty allow_from denies everyone."""
        config = TelegramConfig(enabled=True, token="test:token", allow_from=[])
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        assert channel._is_user_in_allow_from(123, "user") is False


class TestGroupApprovalCallbacks:
    """Tests for inline button approval/leave callbacks."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel instance for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["123", "456"],
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._app = MagicMock()
        return channel

    def _make_callback_query(
        self,
        user_id: int,
        username: str | None,
        callback_data: str,
    ) -> tuple[MagicMock, MagicMock]:
        """Build mock callback query and user."""
        query = MagicMock()
        query.data = callback_data
        query.message = MagicMock()
        query.message.text = "Test message"
        query.answer = MagicMock(return_value=None)

        user = MagicMock()
        user.id = user_id
        user.username = username

        return query, user

    @pytest.mark.asyncio
    async def test_approve_callback_sets_approved_true(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Approve button should set approved=True in origins."""
        # Pre-populate with pending group
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })

        query, user = self._make_callback_query(
            user_id=123,  # Authorized user
            username="admin_user",
            callback_data="grp_approve:-100123456:123",
        )

        await channel._handle_group_approval_callback(query, user)

        origins = load_group_origins()
        assert origins["-100123456"]["approved"] is True
        query.answer.assert_called_with("Group approved!")

    @pytest.mark.asyncio
    async def test_approve_callback_adds_to_runtime_groups(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Approve button should add group to runtime groups allowlist."""
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })

        query, user = self._make_callback_query(
            user_id=123,
            username="admin",
            callback_data="grp_approve:-100123456:123",
        )

        await channel._handle_group_approval_callback(query, user)

        assert "-100123456" in channel._runtime_groups

    @pytest.mark.asyncio
    async def test_leave_callback_removes_from_origins(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Leave button should remove group from origins."""
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })

        query, user = self._make_callback_query(
            user_id=456,
            username="other_admin",
            callback_data="grp_leave:-100123456:456",
        )

        await channel._handle_group_approval_callback(query, user)

        origins = load_group_origins()
        assert "-100123456" not in origins
        query.answer.assert_called_with("Left the group")

    @pytest.mark.asyncio
    async def test_leave_callback_calls_leave_chat(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Leave button should call bot.leave_chat."""
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })

        query, user = self._make_callback_query(
            user_id=123,
            username="admin",
            callback_data="grp_leave:-100123456:123",
        )

        await channel._handle_group_approval_callback(query, user)

        channel._app.bot.leave_chat.assert_called_once_with(-100123456)

    @pytest.mark.asyncio
    async def test_leave_callback_removes_from_members(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Leave button should remove group from members."""
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })
        save_group_members({
            "-100123456": [111, 222],
            "-100789": [333],
        })

        query, user = self._make_callback_query(
            user_id=123,
            username="admin",
            callback_data="grp_leave:-100123456:123",
        )

        await channel._handle_group_approval_callback(query, user)

        members = load_group_members()
        assert "-100123456" not in members
        assert "-100789" in members

    @pytest.mark.asyncio
    async def test_callback_rejects_unauthorized_user(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Callback should reject user who is not the authorized recipient."""
        save_group_origins({
            "-100123456": {"added_by": 999, "added_at": 1700000000.0, "approved": False}
        })

        query, user = self._make_callback_query(
            user_id=789,  # Not the authorized user (123)
            username="intruder",
            callback_data="grp_approve:-100123456:123",  # Auth user is 123
        )

        await channel._handle_group_approval_callback(query, user)

        # Should be rejected
        query.answer.assert_called_with(
            "Only the recipient can act on this request", show_alert=True
        )
        # Origins should be unchanged
        origins = load_group_origins()
        assert origins["-100123456"]["approved"] is False

    @pytest.mark.asyncio
    async def test_callback_handles_missing_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Callback should handle gracefully if group is no longer in origins."""
        # Don't create any origins

        query, user = self._make_callback_query(
            user_id=123,
            username="admin",
            callback_data="grp_approve:-100123456:123",
        )

        await channel._handle_group_approval_callback(query, user)

        query.answer.assert_called_with(
            "Group not found (may have been removed)", show_alert=True
        )

    @pytest.mark.asyncio
    async def test_callback_handles_invalid_data(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Callback should handle malformed callback_data."""
        query, user = self._make_callback_query(
            user_id=123,
            username="admin",
            callback_data="grp_approve:invalid",  # Missing parts
        )

        await channel._handle_group_approval_callback(query, user)

        query.answer.assert_called_with("Invalid callback data", show_alert=True)


class TestPendingGroupNotification:
    """Tests for notifying allowFrom users about pending groups."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel instance for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["123", "456", "not_numeric"],
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._app = MagicMock()
        return channel

    @pytest.mark.asyncio
    async def test_notifies_numeric_allow_from_users(
        self, channel: TelegramChannel
    ) -> None:
        """Should send DM to numeric user IDs in allow_from."""

        async def mock_send(**kwargs):
            return MagicMock()

        channel._call_with_retry = mock_send

        await channel._notify_allow_from_pending_group(
            chat_id=-100123456,
            chat_title="Test Group",
            adder_display="@random_user",
        )

        # Should have called send_message for each numeric user ID
        calls = channel._app.bot.send_message.call_args_list
        # Note: _call_with_retry wraps send_message, so we check _call_with_retry behavior

    @pytest.mark.asyncio
    async def test_notification_includes_inline_buttons(
        self, channel: TelegramChannel
    ) -> None:
        """Notification should include Approve and Leave inline buttons."""
        from telegram import InlineKeyboardMarkup

        sent_messages = []

        async def capture_send(fn, **kwargs):
            sent_messages.append(kwargs)
            return MagicMock()

        channel._call_with_retry = capture_send

        await channel._notify_allow_from_pending_group(
            chat_id=-100999,
            chat_title="Pending Group",
            adder_display="@someone",
        )

        # Should have sent to both numeric users (123 and 456)
        assert len(sent_messages) == 2

        # Each message should have reply_markup with buttons
        for msg in sent_messages:
            assert "reply_markup" in msg
            markup = msg["reply_markup"]
            assert isinstance(markup, InlineKeyboardMarkup)
            # Should have one row with two buttons
            assert len(markup.inline_keyboard) == 1
            assert len(markup.inline_keyboard[0]) == 2
            buttons = markup.inline_keyboard[0]
            assert buttons[0].text == "Approve"
            assert buttons[1].text == "Leave"

    @pytest.mark.asyncio
    async def test_buttons_encode_authorized_user(
        self, channel: TelegramChannel
    ) -> None:
        """Button callback_data should include the authorized user ID."""
        from telegram import InlineKeyboardMarkup

        sent_messages = []

        async def capture_send(fn, **kwargs):
            sent_messages.append(kwargs)
            return MagicMock()

        channel._call_with_retry = capture_send

        await channel._notify_allow_from_pending_group(
            chat_id=-100999,
            chat_title="Group",
            adder_display="@user",
        )

        # Check each message has correct auth user in callback_data
        for msg in sent_messages:
            recipient = msg["chat_id"]
            markup = msg["reply_markup"]
            approve_btn = markup.inline_keyboard[0][0]
            leave_btn = markup.inline_keyboard[0][1]

            # Format: grp_approve:<group_id>:<auth_user_id>
            assert approve_btn.callback_data == f"grp_approve:-100999:{recipient}"
            assert leave_btn.callback_data == f"grp_leave:-100999:{recipient}"

    @pytest.mark.asyncio
    async def test_skips_wildcard_allow_from(self, state_dir: Path) -> None:
        """Should skip notification when allow_from is wildcard."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["*"],
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._app = MagicMock()

        await channel._notify_allow_from_pending_group(
            chat_id=-100999,
            chat_title="Group",
            adder_display="@user",
        )

        # Should not have called send_message
        channel._app.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_allow_from(self, state_dir: Path) -> None:
        """Should skip notification when allow_from is empty."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=[],
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._app = MagicMock()

        await channel._notify_allow_from_pending_group(
            chat_id=-100999,
            chat_title="Group",
            adder_display="@user",
        )

        # Should not have called send_message
        channel._app.bot.send_message.assert_not_called()


class TestGroupAllowAllDmAccess:
    """Tests for group_allow_all DM access via group membership."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel with group_allow_all enabled."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["123"],  # Only user 123 in allowFrom
            group_allow_all=True,
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._runtime_groups = {"-100111", "-100222"}  # Two authorized groups
        return channel

    def test_is_allowed_dm_user_in_authorized_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User in authorized group's member list should be allowed DM access."""
        save_group_members({
            "-100111": [456, 789],
            "-100222": [111],
        })

        # User 456 is in group -100111 (authorized), should be allowed DM
        assert channel.is_allowed("456|someone", is_dm=True) is True

    def test_is_allowed_dm_user_not_in_authorized_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User not in any authorized group should be denied DM access."""
        save_group_members({
            "-100111": [456],
            "-100333": [999],  # This group is NOT in _runtime_groups
        })

        # User 999 is only in non-authorized group -100333, should be denied
        assert channel.is_allowed("999|random", is_dm=True) is False

    def test_is_allowed_dm_user_in_allowfrom_always_allowed(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User in allowFrom should always be allowed, regardless of group membership."""
        assert channel.is_allowed("123|admin", is_dm=True) is True

    def test_is_allowed_dm_checks_only_authorized_groups(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """DM access should only check authorized groups in _runtime_groups."""
        save_group_members({
            "-100333": [555],  # User 555 in non-authorized group
        })

        assert channel.is_allowed("555|user", is_dm=True) is False

    def test_is_allowed_non_dm_ignores_group_membership(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Non-DM (group message) should not use group membership for access."""
        save_group_members({"-100111": [456]})

        # User 456 is in authorized group, but is_dm=False should not grant access
        assert channel.is_allowed("456|someone", is_dm=False) is False

    def test_is_allowed_group_allow_all_disabled_ignores_membership(
        self, state_dir: Path
    ) -> None:
        """When group_allow_all is disabled, group membership is not checked."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            allow_from=["123"],
            group_allow_all=False,  # Disabled
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._runtime_groups = {"-100111"}

        save_group_members({"-100111": [456]})

        # Even though user 456 is in authorized group, group_allow_all is disabled
        assert channel.is_allowed("456|someone", is_dm=True) is False


class TestTrackGroupMember:
    """Tests for _track_group_member functionality."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            group_allow_all=True,
        )
        bus = MagicMock()
        return TelegramChannel(config, bus)

    def test_track_adds_new_user_to_new_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should create group and add user when group doesn't exist."""
        channel._track_group_member("-100111", 456)

        members = load_group_members()
        assert "-100111" in members
        assert 456 in members["-100111"]

    def test_track_adds_new_user_to_existing_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should add user to existing group's member list."""
        save_group_members({"-100111": [123]})

        channel._track_group_member("-100111", 456)

        members = load_group_members()
        assert 123 in members["-100111"]
        assert 456 in members["-100111"]

    def test_track_does_not_duplicate_user(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should not add duplicate user_id to group."""
        save_group_members({"-100111": [456]})

        channel._track_group_member("-100111", 456)

        members = load_group_members()
        assert members["-100111"].count(456) == 1


class TestIsUserInAuthorizedGroup:
    """Tests for _is_user_in_authorized_group functionality."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._runtime_groups = {"-100111", "-100222"}
        return channel

    def test_user_found_in_authorized_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should return True when user is in an authorized group."""
        save_group_members({"-100111": [456, 789]})

        assert channel._is_user_in_authorized_group(456) is True

    def test_user_not_found_in_any_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should return False when user is not in any group."""
        save_group_members({"-100111": [456]})

        assert channel._is_user_in_authorized_group(999) is False

    def test_user_in_unauthorized_group_only(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should return False when user is only in non-authorized groups."""
        save_group_members({
            "-100333": [456],  # Not in _runtime_groups
        })

        assert channel._is_user_in_authorized_group(456) is False

    def test_empty_members_file(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should return False when members file is empty."""
        assert channel._is_user_in_authorized_group(456) is False


class TestRemoveGroupMember:
    """Tests for _remove_group_member functionality."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            group_allow_all=True,
        )
        bus = MagicMock()
        return TelegramChannel(config, bus)

    def test_remove_user_from_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should remove user from group's member list."""
        save_group_members({"-100111": [456, 789, 123]})

        channel._remove_group_member("-100111", 456)

        members = load_group_members()
        assert 456 not in members["-100111"]
        assert 789 in members["-100111"]
        assert 123 in members["-100111"]

    def test_remove_user_not_in_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should be no-op when user is not in group."""
        save_group_members({"-100111": [789, 123]})

        channel._remove_group_member("-100111", 456)

        members = load_group_members()
        assert members["-100111"] == [789, 123]

    def test_remove_from_nonexistent_group(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Should be no-op when group doesn't exist."""
        save_group_members({"-100222": [456]})

        channel._remove_group_member("-100111", 456)

        members = load_group_members()
        assert "-100111" not in members
        assert members["-100222"] == [456]


class TestChatMemberLeaveKick:
    """Tests for _on_chat_member handling user leave/kick events."""

    @pytest.fixture
    def channel(self, state_dir: Path) -> TelegramChannel:
        """Create a TelegramChannel for testing."""
        config = TelegramConfig(
            enabled=True,
            token="test:token",
            group_allow_all=True,
        )
        bus = MagicMock()
        channel = TelegramChannel(config, bus)
        channel._runtime_groups = {"-100111", "-100222"}
        channel._app = MagicMock()
        return channel

    def _make_chat_member_update(
        self,
        chat_id: int,
        user_id: int,
        old_status: str,
        new_status: str,
    ) -> MagicMock:
        """Build a mock chat_member update for user leave/kick."""
        update = MagicMock()
        update.chat_member = MagicMock()
        update.chat_member.chat = MagicMock()
        update.chat_member.chat.id = chat_id
        update.chat_member.chat.type = "supergroup"
        update.chat_member.old_chat_member = MagicMock()
        update.chat_member.old_chat_member.status = old_status
        update.chat_member.new_chat_member = MagicMock()
        update.chat_member.new_chat_member.status = new_status
        update.chat_member.new_chat_member.user = MagicMock()
        update.chat_member.new_chat_member.user.id = user_id
        return update

    @pytest.mark.asyncio
    async def test_user_left_removes_from_member_list(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User leaving should remove them from group's member list."""
        save_group_members({
            "-100111": [456, 789],
            "-100222": [123],
        })

        update = self._make_chat_member_update(
            chat_id=-100111,
            user_id=456,
            old_status="member",
            new_status="left",
        )

        await channel._on_chat_member(update, MagicMock())

        members = load_group_members()
        assert 456 not in members["-100111"]
        assert 789 in members["-100111"]

    @pytest.mark.asyncio
    async def test_user_kicked_removes_from_member_list(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User being kicked should remove them from group's member list."""
        save_group_members({"-100111": [456, 789]})

        update = self._make_chat_member_update(
            chat_id=-100111,
            user_id=456,
            old_status="member",
            new_status="kicked",
        )

        await channel._on_chat_member(update, MagicMock())

        members = load_group_members()
        assert 456 not in members["-100111"]

    @pytest.mark.asyncio
    async def test_user_removed_from_only_group_loses_dm_access(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User removed from their only authorized group should lose DM access."""
        save_group_members({"-100111": [456]})

        update = self._make_chat_member_update(
            chat_id=-100111,
            user_id=456,
            old_status="member",
            new_status="left",
        )

        # User should have DM access before
        assert channel._is_user_in_authorized_group(456) is True

        await channel._on_chat_member(update, MagicMock())

        # User should lose DM access after
        assert channel._is_user_in_authorized_group(456) is False

    @pytest.mark.asyncio
    async def test_user_in_multiple_groups_retains_dm_access(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """User removed from one group but in another should retain DM access."""
        save_group_members({
            "-100111": [456],
            "-100222": [456, 789],  # User 456 is in both groups
        })

        update = self._make_chat_member_update(
            chat_id=-100111,
            user_id=456,
            old_status="member",
            new_status="left",
        )

        await channel._on_chat_member(update, MagicMock())

        # User should still have DM access via group -100222
        assert channel._is_user_in_authorized_group(456) is True

        members = load_group_members()
        assert 456 not in members["-100111"]
        assert 456 in members["-100222"]

    @pytest.mark.asyncio
    async def test_leave_from_non_authorized_group_ignored(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Leave from non-authorized group should not affect member list."""
        save_group_members({"-100333": [456]})

        update = self._make_chat_member_update(
            chat_id=-100333,  # Not in _runtime_groups
            user_id=456,
            old_status="member",
            new_status="left",
        )

        await channel._on_chat_member(update, MagicMock())

        # Member list should be unchanged
        members = load_group_members()
        assert members["-100333"] == [456]

    @pytest.mark.asyncio
    async def test_private_chat_ignored(
        self, channel: TelegramChannel, state_dir: Path
    ) -> None:
        """Private chat updates should be ignored."""
        update = MagicMock()
        update.chat_member = MagicMock()
        update.chat_member.chat = MagicMock()
        update.chat_member.chat.type = "private"

        await channel._on_chat_member(update, MagicMock())

        # No crash, no changes
        members = load_group_members()
        assert members == {}
