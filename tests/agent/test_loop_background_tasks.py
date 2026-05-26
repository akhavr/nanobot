"""Tests for background task error handling in AgentLoop."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus


def _make_loop(tmp_path) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    return loop


@pytest.mark.asyncio
async def test_background_task_failure_is_logged(tmp_path) -> None:
    """Verify logger.exception is called when a background task fails."""
    loop = _make_loop(tmp_path)

    async def failing_task():
        raise ValueError("test failure")

    with patch("nanobot.agent.loop.logger") as mock_logger:
        loop._schedule_background(failing_task())
        # Allow the task to run
        await asyncio.sleep(0.01)

    mock_logger.exception.assert_called_once_with("Background task failed")


@pytest.mark.asyncio
async def test_background_consolidation_failure_logged(tmp_path) -> None:
    """Verify that when a consolidation task fails, the error is properly logged."""
    loop = _make_loop(tmp_path)

    # Mock the consolidator to raise an exception
    async def failing_consolidation(*args, **kwargs):
        raise RuntimeError("consolidation failed")

    loop.consolidator.maybe_consolidate_by_tokens = failing_consolidation

    with patch("nanobot.agent.loop.logger") as mock_logger:
        loop._schedule_background(loop.consolidator.maybe_consolidate_by_tokens(None))
        await asyncio.sleep(0.01)

    mock_logger.exception.assert_called_once_with("Background task failed")


@pytest.mark.asyncio
async def test_background_task_removed_after_failure(tmp_path) -> None:
    """Verify that a failed task is removed from the _background_tasks list."""
    loop = _make_loop(tmp_path)

    async def failing_task():
        raise ValueError("test failure")

    assert len(loop._background_tasks) == 0

    with patch("nanobot.agent.loop.logger"):
        loop._schedule_background(failing_task())
        # Task is added immediately
        assert len(loop._background_tasks) == 1
        # Allow the task to run and complete (fail)
        await asyncio.sleep(0.01)

    # Task should be removed after failure
    assert len(loop._background_tasks) == 0
