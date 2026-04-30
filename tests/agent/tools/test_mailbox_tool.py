"""Tests for MailboxTool — inter-agent messaging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.mailbox import MailboxTool
from nanobot.mailbox import MailboxService
from nanobot.mailbox.config import MailboxConfig


@pytest.fixture
def tmp_mailboxes(tmp_path: Path) -> Path:
    root = tmp_path / "mailboxes"
    root.mkdir()
    return root


def _make_service(root: Path, agent_id: str) -> MailboxService:
    cfg = MailboxConfig(
        enabled=True,
        agent_id=agent_id,
        mailboxes_root=str(root),
        allow_from=["*"],
    )
    bus = MagicMock()
    return MailboxService(config=cfg, bus=bus)


@pytest.fixture
def tool(tmp_mailboxes: Path) -> MailboxTool:
    svc = _make_service(tmp_mailboxes, "alpha")
    return MailboxTool(mailbox_service=svc)


class TestSend:
    @pytest.mark.asyncio
    async def test_send_basic(self, tool: MailboxTool, tmp_mailboxes: Path):
        result = await tool.execute(
            action="send",
            target_agent_id="beta",
            content="Hello Beta",
        )
        assert "sent to beta" in result.lower()
        # Verify filesystem
        inbox = tmp_mailboxes / "beta" / "inbox"
        files = list(inbox.glob("*.msg.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["from"] == "alpha"
        assert data["to"] == "beta"
        assert data["content"]["parts"][0]["text"] == "Hello Beta"

    @pytest.mark.asyncio
    async def test_send_missing_target(self, tool: MailboxTool):
        result = await tool.execute(
            action="send",
            content="Hello",
        )
        assert "target_agent_id is required" in result

    @pytest.mark.asyncio
    async def test_send_missing_content(self, tool: MailboxTool):
        result = await tool.execute(
            action="send",
            target_agent_id="beta",
        )
        assert "content is required" in result

    @pytest.mark.asyncio
    async def test_send_with_session_and_task(self, tool: MailboxTool, tmp_mailboxes: Path):
        result = await tool.execute(
            action="send",
            target_agent_id="beta",
            content="Do this",
            session_id="sess_123",
            task_id="task_456",
            priority="high",
        )
        assert "session=sess_123" in result
        assert "task=task_456" in result
        assert "priority=high" in result
        inbox = tmp_mailboxes / "beta" / "inbox"
        files = list(inbox.glob("*.msg.json"))
        data = json.loads(files[0].read_text())
        assert data["session_id"] == "sess_123"
        assert data["task_id"] == "task_456"
        assert data["priority"] == "high"


class TestCheck:
    @pytest.mark.asyncio
    async def test_check_empty(self, tool: MailboxTool):
        result = await tool.execute(
            action="check",
        )
        assert "No messages" in result

    @pytest.mark.asyncio
    async def test_check_returns_messages(self, tool: MailboxTool, tmp_mailboxes: Path):
        # Pre-populate inbox via beta's service
        beta_svc = _make_service(tmp_mailboxes, "beta")
        await beta_svc.send_message(
            target_agent_id="alpha",
            content="Hello Alpha",
        )
        result = await tool.execute(action="check")
        assert "From beta" in result
        assert "Hello Alpha" in result
        # Should have moved to processed
        inbox = tmp_mailboxes / "alpha" / "inbox"
        assert len(list(inbox.glob("*.msg.json"))) == 0
        processed = tmp_mailboxes / "alpha" / "processed"
        assert len(list(processed.glob("*.msg.json"))) == 1

    @pytest.mark.asyncio
    async def test_check_with_session_filter(self, tool: MailboxTool, tmp_mailboxes: Path):
        beta_svc = _make_service(tmp_mailboxes, "beta")
        await beta_svc.send_message(
            target_agent_id="alpha",
            content="Msg A",
            session_id="sess_a",
        )
        await beta_svc.send_message(
            target_agent_id="alpha",
            content="Msg B",
            session_id="sess_b",
        )
        result = await tool.execute(action="check", session_id="sess_a")
        assert "Msg A" in result
        assert "Msg B" not in result

    @pytest.mark.asyncio
    async def test_check_limit(self, tool: MailboxTool, tmp_mailboxes: Path):
        beta_svc = _make_service(tmp_mailboxes, "beta")
        for i in range(5):
            await beta_svc.send_message(
                target_agent_id="alpha",
                content=f"Msg {i}",
            )
        result = await tool.execute(action="check", limit=2)
        # Should show only 2 messages
        assert result.count("Msg ") == 2


class TestListAgents:
    @pytest.mark.asyncio
    async def test_list_agents_empty_registry(self, tool: MailboxTool):
        result = await tool.execute(action="list_agents")
        assert "No online agents found" in result

    @pytest.mark.asyncio
    async def test_list_agents_online(self, tool: MailboxTool, tmp_mailboxes: Path):
        registry = {
            "alpha": {
                "agent_id": "alpha",
                "description": "Test Alpha",
                "status": "idle",
                "capabilities": ["coding"],
                "current_tasks": [],
            },
            "beta": {
                "agent_id": "beta",
                "description": "Test Beta",
                "status": "busy",
                "capabilities": ["research"],
                "current_tasks": ["task_1"],
            },
            "gamma": {
                "agent_id": "gamma",
                "status": "offline",
            },
        }
        registry_path = tmp_mailboxes / "_registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        result = await tool.execute(action="list_agents")
        assert "alpha" in result
        assert "Test Alpha" in result
        assert "beta" in result
        assert "task_1" in result
        assert "gamma" not in result  # offline


class TestParameters:
    def test_tool_name(self, tool: MailboxTool):
        assert tool.name == "mailbox"

    def test_tool_schema(self, tool: MailboxTool):
        schema = tool.to_schema()
        assert schema["function"]["name"] == "mailbox"
        props = schema["function"]["parameters"]["properties"]
        assert "action" in props
        assert "target_agent_id" in props
        assert "session_id" in props
        assert "task_id" in props
        assert "priority" in props

    def test_validate_params(self, tool: MailboxTool):
        errors = tool.validate_params({"action": "check", "filter": "invalid"})
        assert len(errors) > 0
