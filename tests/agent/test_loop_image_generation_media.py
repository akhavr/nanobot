from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import set_config_path
from nanobot.config.schema import ImageGenerationToolConfig, ProviderConfig, ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.providers.image_generation import GeneratedImageResponse

PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeImageClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def generate(self, **kwargs: Any) -> GeneratedImageResponse:
        return GeneratedImageResponse(images=[PNG_DATA_URL], content="", raw={})


@pytest.mark.asyncio
async def test_outbound_carries_generated_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated images are auto-attached to OutboundMessage when not delivered via message tool."""
    set_config_path(tmp_path / "config.json")
    monkeypatch.setattr(
        "nanobot.agent.tools.image_generation.get_image_gen_provider",
        lambda name: FakeImageClient if name == "openrouter" else None,
    )
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call_img",
                        name="generate_image",
                        arguments={"prompt": "draw a tiny icon"},
                    )
                ],
            ),
            LLMResponse(content="Done", finish_reason="stop"),
        ]
    )
    provider.chat_stream_with_retry = AsyncMock()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        tools_config=ToolsConfig(
            image_generation=ImageGenerationToolConfig(enabled=True),
        ),
        image_generation_provider_config=ProviderConfig(api_key="sk-or-test"),
    )
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="chat-image",
            content="draw an icon",
        )
    )

    assert result is not None
    assert result.content == "Done"
    # Generated images should be auto-attached to OutboundMessage
    assert len(result.media) == 1
    assert result.media[0].endswith(".png")


@pytest.mark.asyncio
async def test_collect_undelivered_generated_media(tmp_path: Path) -> None:
    """_collect_undelivered_generated_media excludes paths delivered via message tool."""
    from nanobot.agent.tools.image_generation import ImageGenerationTool
    from nanobot.agent.tools.message import MessageTool
    from nanobot.config.schema import ImageGenerationToolConfig

    set_config_path(tmp_path / "config.json")

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    # Manually register tools and set their state
    img_tool = ImageGenerationTool(
        workspace=tmp_path,
        config=ImageGenerationToolConfig(enabled=True),
    )
    msg_tool = MessageTool(
        send_callback=bus.publish_outbound,
        workspace=tmp_path,
    )
    loop.tools.register(img_tool)
    loop.tools.register(msg_tool)

    # Simulate: 2 images generated, 1 delivered
    img_tool.start_turn()
    img_tool._turn_generated_media_var.set(("/path/to/img1.png", "/path/to/img2.png"))

    msg_tool.start_turn()
    msg_tool._turn_delivered_media_var.set(("/path/to/img1.png",))

    # Should return only undelivered images
    undelivered = loop._collect_undelivered_generated_media()
    assert undelivered == ["/path/to/img2.png"]


@pytest.mark.asyncio
async def test_telegram_channel_sends_photo_for_media(tmp_path: Path) -> None:
    """Telegram channel should call send_photo when OutboundMessage.media contains image paths."""
    from nanobot.bus.events import OutboundMessage
    from nanobot.channels.telegram import TelegramChannel, TelegramConfig

    # Create a test image file
    test_image = tmp_path / "test.png"
    test_image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
        b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
        b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    config = TelegramConfig(enabled=True, token="test-token")
    bus = MessageBus()
    channel = TelegramChannel(config, bus)

    # Mock the bot application with AsyncMock for async methods
    mock_app = MagicMock()
    mock_bot = MagicMock()
    mock_bot.send_photo = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_app.bot = mock_bot
    channel._app = mock_app

    msg = OutboundMessage(
        channel="telegram",
        chat_id="12345",
        content="Here is your image",
        media=[str(test_image)],
    )

    await channel.send(msg)

    # Verify send_photo was called with the image
    mock_bot.send_photo.assert_called_once()
    call_kwargs = mock_bot.send_photo.call_args.kwargs
    assert call_kwargs["chat_id"] == 12345
    assert "photo" in call_kwargs
