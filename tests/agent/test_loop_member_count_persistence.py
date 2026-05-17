"""Test that AgentLoop persists member_count to session.metadata for Dream processing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop, TurnContext, TurnState
from nanobot.agent.memory import MemoryStore
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


@pytest.fixture
def agent_loop(tmp_path: Path, mock_provider):
    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = MagicMock()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=mock_provider,
            workspace=tmp_path,
        )
    return loop


@pytest.mark.asyncio
async def test_state_restore_persists_member_count_to_session_metadata(agent_loop):
    """Verify member_count from msg.metadata is persisted to session.metadata."""
    # Create a message with member_count in metadata
    msg = InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="-100123456",
        content="Hello",
        metadata={"member_count": 5},
    )

    # Create session and turn context
    session = agent_loop.sessions.get_or_create("telegram:-100123456")
    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100123456",
        state=TurnState.RESTORE,
        turn_id="test-turn-1",
    )

    # Run the restore state
    await agent_loop._state_restore(ctx)

    # Verify member_count was persisted to session.metadata
    assert session.metadata.get("member_count") == 5


@pytest.mark.asyncio
async def test_state_restore_persists_member_count_2_for_private_chat(agent_loop):
    """Verify member_count=2 (1:1 with bot) is persisted correctly."""
    msg = InboundMessage(
        channel="telegram",
        sender_id="user456",
        chat_id="-100789",
        content="Private message",
        metadata={"member_count": 2},
    )

    session = agent_loop.sessions.get_or_create("telegram:-100789")
    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100789",
        state=TurnState.RESTORE,
        turn_id="test-turn-2",
    )

    await agent_loop._state_restore(ctx)

    # Verify member_count=2 is persisted (private chat with bot)
    assert session.metadata.get("member_count") == 2


@pytest.mark.asyncio
async def test_state_restore_does_not_fail_without_member_count(agent_loop):
    """Verify processing works when member_count is not present."""
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="direct",
        content="CLI message",
        metadata={},  # No member_count
    )

    session = agent_loop.sessions.get_or_create("cli:direct")
    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="cli:direct",
        state=TurnState.RESTORE,
        turn_id="test-turn-3",
    )

    await agent_loop._state_restore(ctx)

    # Should not have member_count in metadata
    assert "member_count" not in session.metadata


@pytest.mark.asyncio
async def test_state_restore_updates_member_count_when_changed(agent_loop):
    """Verify member_count is updated when the count changes."""
    session = agent_loop.sessions.get_or_create("telegram:-100999")
    # Simulate previous member_count
    session.metadata["member_count"] = 3

    # New message with updated member_count (someone joined)
    msg = InboundMessage(
        channel="telegram",
        sender_id="user",
        chat_id="-100999",
        content="New member joined",
        metadata={"member_count": 4},
    )

    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100999",
        state=TurnState.RESTORE,
        turn_id="test-turn-4",
    )

    await agent_loop._state_restore(ctx)

    # Verify member_count was updated
    assert session.metadata.get("member_count") == 4


@pytest.mark.asyncio
async def test_state_restore_persists_user_id_to_session_metadata(agent_loop):
    """Verify user_id from msg.metadata is persisted to session.metadata."""
    msg = InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="-100123456",
        content="Hello",
        metadata={"user_id": "user123"},
    )

    session = agent_loop.sessions.get_or_create("telegram:-100123456")
    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100123456",
        state=TurnState.RESTORE,
        turn_id="test-turn-5",
    )

    await agent_loop._state_restore(ctx)

    assert session.metadata.get("user_id") == "user123"


@pytest.mark.asyncio
async def test_state_restore_updates_user_id_when_changed(agent_loop):
    """Verify user_id is updated when the sender changes."""
    session = agent_loop.sessions.get_or_create("telegram:-100999")
    session.metadata["user_id"] = "old-user"

    msg = InboundMessage(
        channel="telegram",
        sender_id="user456",
        chat_id="-100999",
        content="New message",
        metadata={"user_id": "user456"},
    )

    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100999",
        state=TurnState.RESTORE,
        turn_id="test-turn-6",
    )

    await agent_loop._state_restore(ctx)

    assert session.metadata.get("user_id") == "user456"


@pytest.mark.asyncio
async def test_multi_user_session_uses_user_specific_memory_store(tmp_path, mock_provider):
    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = MagicMock()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=mock_provider,
            workspace=tmp_path,
            multi_user=True,
        )

    loop.context.memory = MemoryStore(tmp_path)
    session = loop.sessions.get_or_create("telegram:-100123456")
    session.metadata["user_id"] = "alice"

    store = loop.context.memory_store_for_session_metadata(session.metadata)

    assert store.memory_file.name == "MEMORY_alice.md"
    assert store.history_file.name == "history_alice.jsonl"


@pytest.mark.asyncio
async def test_state_build_persists_user_id_before_memory_lookup(tmp_path, mock_provider):
    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = MagicMock()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=mock_provider,
            workspace=tmp_path,
            multi_user=True,
        )

    loop.context.memory = MemoryStore(tmp_path)
    session = loop.sessions.get_or_create("telegram:-100123456")
    msg = InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="-100123456",
        content="Hello",
        metadata={"user_id": "alice"},
    )
    ctx = TurnContext(
        msg=msg,
        session=session,
        session_key="telegram:-100123456",
        state=TurnState.BUILD,
        turn_id="test-turn-7",
    )

    observed_user_ids: list[str | None] = []
    original_memory_store_for_session = loop.context.memory_store_for_session_metadata

    def wrapped_memory_store_for_session(session_arg):
        observed_user_ids.append(session_arg.get("user_id"))
        return original_memory_store_for_session(session_arg)

    loop.context.memory_store_for_session_metadata = wrapped_memory_store_for_session  # type: ignore[assignment]
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)

    await loop._state_build(ctx)

    assert observed_user_ids
    assert set(observed_user_ids) == {"alice"}
    assert session.metadata.get("user_id") == "alice"
