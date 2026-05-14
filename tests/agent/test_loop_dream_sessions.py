"""Test that AgentLoop correctly passes sessions to Dream."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_agent_loop_passes_sessions_to_dream(tmp_path):
    """Verify AgentLoop initializes Dream with sessions parameter."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = MagicMock()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)

    # Verify Dream received the sessions parameter
    assert loop.dream.sessions is not None
    assert loop.dream.sessions is loop.sessions
