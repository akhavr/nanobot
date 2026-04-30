"""Tests for MailboxManager (file operations) and MailboxService."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.mailbox import MailboxService
from nanobot.mailbox.config import MailboxConfig
from nanobot.mailbox.manager import MailboxManager


# --- MailboxManager Tests ---

@pytest.fixture
def root(tmp_path: Path) -> Path:
    mailboxes = tmp_path / "mailboxes"
    mailboxes.mkdir()
    return mailboxes


@pytest.fixture
def mgr(root: Path) -> MailboxManager:
    return MailboxManager(root)


class TestRegister:
    def test_register_creates_agent_entry(self, mgr: MailboxManager, root: Path):
        card = {"agent_id": "researcher", "description": "test agent"}
        mgr.register("researcher", card)
        registry = json.loads((root / "_registry.json").read_text())
        assert "researcher" in registry
        assert registry["researcher"]["agent_id"] == "researcher"

    def test_register_creates_directories(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder"})
        assert (root / "coder" / "inbox").is_dir()
        assert (root / "coder" / "processed").is_dir()

    def test_register_overwrite(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder", "status": "idle"})
        mgr.register("coder", {"agent_id": "coder", "status": "busy"})
        registry = json.loads((root / "_registry.json").read_text())
        assert registry["coder"]["status"] == "busy"


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder"})
        before = json.loads((root / "_registry.json").read_text())["coder"]["last_heartbeat"]
        time.sleep(0.01)
        mgr.heartbeat("coder")
        after = json.loads((root / "_registry.json").read_text())["coder"]["last_heartbeat"]
        assert after >= before


class TestUpdateStatus:
    def test_update_status(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder", "status": "idle"})
        mgr.update_status("coder", "busy", current_tasks=["task_1"])
        registry = json.loads((root / "_registry.json").read_text())
        assert registry["coder"]["status"] == "busy"
        assert registry["coder"]["current_tasks"] == ["task_1"]


class TestSendAndPoll:
    def test_send_creates_message_file(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder"})
        mgr.register("researcher", {"agent_id": "researcher"})
        msg = {"type": "message", "content": {"parts": [{"type": "text", "text": "hello"}]}}
        mgr.send("researcher", "coder", msg)
        files = list((root / "coder" / "inbox").glob("*.msg.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["from"] == "researcher"
        assert data["to"] == "coder"

    def test_poll_returns_new_messages_sorted(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder"})
        mgr.register("a1", {"agent_id": "a1"})
        mgr.register("a2", {"agent_id": "a2"})
        mgr.send("a1", "coder", {"type": "message", "content": {"parts": []}})
        time.sleep(0.01)
        mgr.send("a2", "coder", {"type": "task", "content": {"parts": []}})
        messages = mgr.poll("coder")
        assert len(messages) == 2
        assert messages[0]["from"] == "a1"
        assert messages[1]["from"] == "a2"

    def test_mark_processed_moves_file(self, mgr: MailboxManager, root: Path):
        mgr.register("coder", {"agent_id": "coder"})
        mgr.register("researcher", {"agent_id": "researcher"})
        mgr.send("researcher", "coder", {"type": "message", "content": {"parts": []}})
        messages = mgr.poll("coder")
        mgr.mark_processed("coder", messages[0]["_filename"])
        assert len(list((root / "coder" / "inbox").glob("*.msg.json"))) == 0
        assert len(list((root / "coder" / "processed").glob("*.msg.json"))) == 1


class TestListAndGetAgents:
    def test_list_online_agents(self, mgr: MailboxManager, root: Path):
        mgr.register("researcher", {"agent_id": "researcher", "status": "idle"})
        mgr.register("coder", {"agent_id": "coder", "status": "busy"})
        agents = mgr.list_online_agents()
        ids = {a["agent_id"] for a in agents}
        assert ids == {"researcher", "coder"}

    def test_get_agent_not_found(self, mgr: MailboxManager):
        assert mgr.get_agent("nonexistent") is None


# --- MailboxService & integration Tests ---

def _make_service(bus, root, agent_id, **overrides):
    """Create a MailboxService with sensible defaults."""
    cfg = {
        "enabled": True,
        "agent_id": agent_id,
        "description": f"Test agent {agent_id}",
        "mailboxes_root": str(root),
        "poll_interval": 0.05,
        "allow_from": ["*"],
    }
    cfg.update(overrides)
    return MailboxService(MailboxConfig.model_validate(cfg), bus)


class TestConfig:
    def test_default_config(self):
        cfg = MailboxConfig()
        assert cfg.enabled is False
        assert cfg.agent_id == ""
        assert cfg.allow_from == ["*"]

    def test_config_from_dict(self):
        """Validate from dict with camelCase aliases."""
        cfg = MailboxConfig.model_validate({
            "enabled": True,
            "agentId": "coder",
            "allowFrom": ["researcher"],
            "pollInterval": 2.0,
        })
        assert cfg.enabled is True
        assert cfg.agent_id == "coder"
        assert cfg.allow_from == ["researcher"]
        assert cfg.poll_interval == 2.0


class TestStartAndStop:
    @pytest.mark.asyncio
    async def test_start_registers_agent(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder", description="A coding agent")
        try:
            await svc.start()
            card = svc.manager.get_agent("coder")
            assert card is not None
            assert card["agent_id"] == "coder"
            assert card["description"] == "A coding agent"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_stop_marks_offline(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        svc.stop()
        card = svc.manager.get_agent("coder")
        assert card["status"] == "offline"

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        svc.stop()
        # Calling stop() a second time should not raise
        svc.stop()


class TestPollAndInbound:
    @pytest.mark.asyncio
    async def test_poll_delivers_inbound_message(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        try:
            # Simulate researcher sending a message to coder
            svc.manager.send("researcher", "coder", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "Hello from researcher"}]},
            })
            # Manually trigger a poll
            await svc._poll_once()
            bus.publish_inbound.assert_awaited_once()
            inbound: InboundMessage = bus.publish_inbound.call_args[0][0]
            assert inbound.channel == "mailbox"
            assert inbound.sender_id == "researcher"
            assert inbound.chat_id == "researcher"
            assert "Hello from researcher" in inbound.content
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_poll_with_callback_routes_to_original_session(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        try:
            # Message with callback routing info
            svc.manager.send("researcher", "coder", {
                "type": "message",
                "from": "researcher",
                "content": {"parts": [{"type": "text", "text": "Task result"}]},
                "callback": {
                    "channel": "feishu",
                    "chat_id": "oc_abc123",
                    "session_id": "sess_xyz",
                },
            })
            await svc._poll_once()
            bus.publish_inbound.assert_awaited_once()
            inbound: InboundMessage = bus.publish_inbound.call_args[0][0]
            assert inbound.channel == "feishu"
            assert inbound.chat_id == "oc_abc123"
            assert inbound.session_key_override == "sess_xyz"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_poll_respects_allow_from(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder", allow_from=["researcher"])
        await svc.start()
        try:
            # Message from a stranger should be blocked
            svc.manager.send("stranger", "coder", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "Hi"}]},
            })
            await svc._poll_once()
            bus.publish_inbound.assert_not_awaited()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_poll_marks_processed(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        try:
            svc.manager.send("researcher", "coder", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "hello"}]},
            })
            await svc._poll_once()
            # Inbox should be empty
            assert len(svc.manager.poll("coder")) == 0
            # Processed dir should have the file
            processed = list((root / "coder" / "processed").glob("*.msg.json"))
            assert len(processed) == 1
        finally:
            svc.stop()


class TestSend:
    @pytest.mark.asyncio
    async def test_send_writes_to_target_mailbox(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        try:
            msg = OutboundMessage(
                channel="mailbox",
                chat_id="researcher",
                content="Hello researcher",
            )
            await svc.send(msg)
            # Check target inbox
            files = list((root / "researcher" / "inbox").glob("*.msg.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["from"] == "coder"
            assert data["to"] == "researcher"
            assert data["ttl"] == 2  # started at 3, decremented to 2
            assert data["trace"] == ["coder"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_send_uses_existing_message_tool(self, root: Path):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        svc = _make_service(bus, root, "coder")
        await svc.start()
        try:
            # Simulate what MessageTool would produce — metadata carrying task info
            msg = OutboundMessage(
                channel="mailbox",
                chat_id="researcher",
                content="Here is the result",
                metadata={
                    "mailbox_task": "analyze_code",
                    "mailbox_type": "task_result",
                },
            )
            await svc.send(msg)
            files = list((root / "researcher" / "inbox").glob("*.msg.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["type"] == "task_result"
            assert data["task"] == "analyze_code"
        finally:
            svc.stop()


class TestTwoAgentCommunication:
    @pytest.mark.asyncio
    async def test_agent_a_sends_agent_b_receives(self, root: Path):
        bus_a = MagicMock()
        bus_a.publish_inbound = AsyncMock()
        bus_b = MagicMock()
        bus_b.publish_inbound = AsyncMock()

        svc_a = _make_service(bus_a, root, "alpha")
        svc_b = _make_service(bus_b, root, "beta")
        await svc_a.start()
        await svc_b.start()
        try:
            # Alpha sends to Beta
            out = OutboundMessage(
                channel="mailbox",
                chat_id="beta",
                content="Hello Beta, this is Alpha",
            )
            await svc_a.send(out)

            # Beta polls and receives
            await svc_b._poll_once()
            bus_b.publish_inbound.assert_awaited_once()
            inbound: InboundMessage = bus_b.publish_inbound.call_args[0][0]
            assert inbound.channel == "mailbox"
            assert inbound.sender_id == "alpha"
            assert "Hello Beta" in inbound.content
        finally:
            svc_a.stop()
            svc_b.stop()

    @pytest.mark.asyncio
    async def test_agent_b_sends_response_back(self, root: Path):
        bus_a = MagicMock()
        bus_a.publish_inbound = AsyncMock()
        bus_b = MagicMock()
        bus_b.publish_inbound = AsyncMock()

        svc_a = _make_service(bus_a, root, "alpha")
        svc_b = _make_service(bus_b, root, "beta")
        await svc_a.start()
        await svc_b.start()
        try:
            # Beta sends to Alpha
            out = OutboundMessage(
                channel="mailbox",
                chat_id="alpha",
                content="Response from Beta",
            )
            await svc_b.send(out)

            # Alpha polls and receives
            await svc_a._poll_once()
            bus_a.publish_inbound.assert_awaited_once()
            inbound: InboundMessage = bus_a.publish_inbound.call_args[0][0]
            assert inbound.sender_id == "beta"
            assert "Response from Beta" in inbound.content
        finally:
            svc_a.stop()
            svc_b.stop()

    @pytest.mark.asyncio
    async def test_callback_routes_to_original_feishu_session(self, root: Path):
        """A sends task with callback -> B receives -> B responds with callback -> A receives routed to feishu session."""
        bus_a = MagicMock()
        bus_a.publish_inbound = AsyncMock()
        bus_b = MagicMock()
        bus_b.publish_inbound = AsyncMock()

        svc_a = _make_service(bus_a, root, "alpha")
        svc_b = _make_service(bus_b, root, "beta")
        await svc_a.start()
        await svc_b.start()
        try:
            # Alpha sends a task to Beta with a callback to feishu
            out = OutboundMessage(
                channel="mailbox",
                chat_id="beta",
                content="Analyze this code",
                metadata={
                    "mailbox_task": "code_review",
                    "mailbox_callback": {
                        "channel": "feishu",
                        "chat_id": "oc_feishu_chat",
                        "session_id": "sess_feishu_123",
                    },
                },
            )
            await svc_a.send(out)

            # Beta polls and receives
            await svc_b._poll_once()
            bus_b.publish_inbound.assert_awaited_once()
            inbound_b: InboundMessage = bus_b.publish_inbound.call_args[0][0]
            assert "Analyze this code" in inbound_b.content

            # Beta responds, echoing the callback
            out_b = OutboundMessage(
                channel="mailbox",
                chat_id="alpha",
                content="Code review complete",
                metadata={
                    "mailbox_task": "code_review",
                    "mailbox_callback": {
                        "channel": "feishu",
                        "chat_id": "oc_feishu_chat",
                        "session_id": "sess_feishu_123",
                    },
                },
            )
            await svc_b.send(out_b)

            # Alpha polls — the callback routes to feishu channel
            await svc_a._poll_once()
            bus_a.publish_inbound.assert_awaited_once()
            inbound_a: InboundMessage = bus_a.publish_inbound.call_args[0][0]
            assert inbound_a.channel == "feishu"
            assert inbound_a.chat_id == "oc_feishu_chat"
            assert inbound_a.session_key_override == "sess_feishu_123"
            assert "Code review complete" in inbound_a.content
        finally:
            svc_a.stop()
            svc_b.stop()

    @pytest.mark.asyncio
    async def test_anti_loop_trace(self, root: Path):
        """Message with existing trace preserved in metadata."""
        bus_a = MagicMock()
        bus_a.publish_inbound = AsyncMock()

        svc_a = _make_service(bus_a, root, "alpha")
        await svc_a.start()
        try:
            # Send with pre-existing trace
            out = OutboundMessage(
                channel="mailbox",
                chat_id="beta",
                content="Forwarding along",
                metadata={
                    "mailbox_trace": ["origin", "relay1"],
                    "mailbox_ttl": 5,
                },
            )
            await svc_a.send(out)
            files = list((root / "beta" / "inbox").glob("*.msg.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            # Trace should be: origin, relay1, alpha
            assert data["trace"] == ["origin", "relay1", "alpha"]
            assert data["ttl"] == 4  # decremented from 5
        finally:
            svc_a.stop()

    @pytest.mark.asyncio
    async def test_direct_reply_to_sender_allowed(self, root: Path):
        """Agent can reply to sender even when sender is in trace."""
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()

        svc = _make_service(bus, root, "beta")
        await svc.start()
        try:
            # Simulate the final response path: metadata carries inbound
            # trace and sender info from the received message.
            out = OutboundMessage(
                channel="mailbox",
                chat_id="alpha",
                content="Reply from Beta",
                metadata={
                    "mailbox_trace": ["alpha"],
                    "mailbox_from": "alpha",
                },
            )
            await svc.send(out)
            files = list((root / "alpha" / "inbox").glob("*.msg.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["to"] == "alpha"
            assert data["from"] == "beta"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_circular_forward_to_non_sender_blocked(self, root: Path):
        """Forwarding to a non-sender in the trace is still blocked."""
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()

        svc = _make_service(bus, root, "beta")
        await svc.start()
        try:
            # Beta tries to forward to "origin" who is in trace but is NOT
            # the direct sender (alpha is the sender).
            out = OutboundMessage(
                channel="mailbox",
                chat_id="origin",
                content="Should be blocked",
                metadata={
                    "mailbox_trace": ["origin", "alpha"],
                    "mailbox_from": "alpha",
                },
            )
            await svc.send(out)
            files = list((root / "origin" / "inbox").glob("*.msg.json"))
            assert len(files) == 0  # blocked by circular route check
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_response_does_not_trigger_agent_loop(self, root: Path):
        """When a response arrives (our ID in trace), it is marked processed
        but does NOT trigger a full agent-loop cycle (prevents ping-pong)."""
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()

        svc = _make_service(bus, root, "secretary")
        await svc.start()
        try:
            # Simulate PM responding to secretary's original request.
            # The trace contains "secretary" — the receiver's own ID.
            svc.manager.send("pm", "secretary", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "Task done"}]},
                "trace": ["secretary", "pm"],
            })
            await svc._poll_once()
            # No InboundMessage published — response is silently consumed.
            bus.publish_inbound.assert_not_awaited()
            # Message is still moved to processed.
            assert len(list((root / "secretary" / "inbox").glob("*.msg.json"))) == 0
            assert len(list((root / "secretary" / "processed").glob("*.msg.json"))) == 1
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_new_request_is_processed_normally(self, root: Path):
        """A fresh request (receiver NOT in trace) triggers the agent loop."""
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()

        svc = _make_service(bus, root, "pm")
        await svc.start()
        try:
            # Secretary sends a new request — trace only has ["secretary"],
            # PM is NOT in trace, so it's a new request.
            svc.manager.send("secretary", "pm", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "Do this task"}]},
                "trace": ["secretary"],
            })
            await svc._poll_once()
            bus.publish_inbound.assert_awaited_once()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_allow_from_blocks_unauthorized(self, root: Path):
        """allowFrom=['coder'] blocks stranger."""
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()

        svc = _make_service(bus, root, "coder", allow_from=["coder"])
        await svc.start()
        try:
            # Stranger sends to coder
            svc.manager.send("stranger", "coder", {
                "type": "message",
                "content": {"parts": [{"type": "text", "text": "Spam"}]},
            })
            await svc._poll_once()
            bus.publish_inbound.assert_not_awaited()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_registry_discovery(self, root: Path):
        """Both agents register and can discover each other."""
        bus_a = MagicMock()
        bus_a.publish_inbound = AsyncMock()
        bus_b = MagicMock()
        bus_b.publish_inbound = AsyncMock()

        svc_a = _make_service(bus_a, root, "alpha", description="Agent Alpha")
        svc_b = _make_service(bus_b, root, "beta", description="Agent Beta")
        await svc_a.start()
        await svc_b.start()
        try:
            agents = svc_a.manager.list_online_agents()
            ids = {a["agent_id"] for a in agents}
            assert "alpha" in ids
            assert "beta" in ids

            # Alpha can look up Beta's card
            beta_card = svc_a.manager.get_agent("beta")
            assert beta_card is not None
            assert beta_card["description"] == "Agent Beta"

            # Beta can look up Alpha's card
            alpha_card = svc_b.manager.get_agent("alpha")
            assert alpha_card is not None
            assert alpha_card["description"] == "Agent Alpha"
        finally:
            svc_a.stop()
            svc_b.stop()
