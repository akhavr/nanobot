"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.telegram_state import (
    load_group_members,
    load_group_origins,
    save_group_members,
    save_group_origins,
)
from nanobot.command.builtin import build_help_text
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.security.network import validate_url_target
from nanobot.utils.helpers import split_message

TELEGRAM_MAX_MESSAGE_LEN = 4000  # Telegram message character limit
# Telegram's actual API limit is 4096; we split raw markdown at 4000 as a
# safety margin for mid-stream edits (plain text).  For _stream_end, we
# convert to HTML first and then split at the true 4096-char boundary so
# the final rendered message never overflows.
TELEGRAM_HTML_MAX_LEN = 4096
TELEGRAM_REPLY_CONTEXT_MAX_LEN = TELEGRAM_MAX_MESSAGE_LEN  # Max length for reply context in user message


def _escape_telegram_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tool_hint_to_telegram_blockquote(text: str) -> str:
    """Render tool hints as an expandable blockquote (collapsed by default)."""
    return f"<blockquote expandable>{_escape_telegram_html(text)}</blockquote>" if text else ""


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'~~(.+?)~~', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def _strip_md_block(text: str) -> str:
    """Strip block-level and inline markdown for readable plain-text preview.

    Used during streaming mid-edits so users see clean text instead of raw
    markdown syntax while the response is still being generated.
    """
    # Code blocks -> just the code
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', r'\1', text)
    # Headers -> plain text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    # Bold / italic / strikethrough
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Bullet lists
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    # Numbered lists (normalize spacing)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)
    return text


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return '\n'.join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([''] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return '  '.join(f'{c}{" " * (w - dw(c))}' for c, w in zip(cells, widths))

    out = [dr(rows[0])]
    out.append('  '.join('─' * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return '\n'.join(out)


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 1.5. Convert markdown tables to box-drawing (reuse code_block placeholders)
    lines = text.split('\n')
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r'^\s*\|.+\|', lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r'^\s*\|.+\|', lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != '\n'.join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = '\n'.join(rebuilt)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> <b>Title</b> (preserve visual hierarchy)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'⟪B⟫\1⟪/B⟫', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = _escape_telegram_html(text)

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 10.5. Numbered lists  1. item -> 1. item (keep number, normalize indent)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    # 13. Restore header bold markers (inserted in step 3, after HTML escaping)
    text = text.replace('⟪B⟫', '<b>').replace('⟪/B⟫', '</b>')

    return text


_SEND_MAX_RETRIES = 3
_SEND_RETRY_BASE_DELAY = 0.5  # seconds, doubled each retry
_STREAM_EDIT_INTERVAL_DEFAULT = 0.6  # min seconds between edit_message_text calls
_SEEN_GROUP_PRUNE_DAYS = 30  # prune seen groups older than this


def _get_groups_file() -> Path:
    """Return the path to telegram_groups.json."""
    return Path.home() / ".nanobot" / "telegram_groups.json"


def _load_groups_data() -> dict[str, Any]:
    """Load telegram groups data from JSON file."""
    path = _get_groups_file()
    if not path.exists():
        return {"allowed": [], "seen": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"allowed": [], "seen": []}


def _save_groups_data(data: dict[str, Any]) -> None:
    """Save telegram groups data to JSON file."""
    path = _get_groups_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _prune_old_seen_groups(data: dict[str, Any]) -> bool:
    """Remove seen groups older than _SEEN_GROUP_PRUNE_DAYS. Returns True if pruned."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_SEEN_GROUP_PRUNE_DAYS)
    original_count = len(data.get("seen", []))
    pruned = []
    for entry in data.get("seen", []):
        try:
            last_seen = datetime.fromisoformat(entry.get("last_seen", ""))
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if last_seen >= cutoff:
                pruned.append(entry)
        except (ValueError, TypeError):
            pass
    data["seen"] = pruned
    return len(pruned) < original_count


def _format_relative_time(iso_str: str) -> str:
    """Format an ISO timestamp as relative time (e.g., '2h ago')."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        if minutes > 0:
            return f"{minutes}m ago"
        return "just now"
    except (ValueError, TypeError):
        return "unknown"


def _load_persisted_groups() -> list[str]:
    """Load persisted group IDs from the JSON file."""
    path = _get_groups_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = data.get("allowed", [])
        # Handle both string format and dict format for backward compatibility
        return [str(g) if isinstance(g, str) else str(g.get("id", "")) for g in allowed]
    except (json.JSONDecodeError, OSError):
        return []


def _save_persisted_groups(groups: list[str]) -> None:
    """Save group IDs to the JSON file.

    Preserves the 'seen' section if present, only updates 'allowed'.
    """
    path = _get_groups_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Load existing data to preserve 'seen' section
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    existing["allowed"] = groups
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_group_id(text: str) -> str | None:
    """Extract and validate a group ID from user input. Returns normalized ID or None."""
    text = text.strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return text
    return None


def _text_hash(text: str) -> str:
    """Hash first 200 chars of text for content matching."""
    import hashlib
    return hashlib.sha256(text[:200].encode()).hexdigest()[:16]


class MessageIndex:
    """Index mapping sent messages to sessions for eval capture lookup."""

    def __init__(self, workspace_path: Path, max_entries: int = 1000):
        self.workspace_path = Path(workspace_path)
        self.max_entries = max_entries
        self._index_path = self.workspace_path / "evals" / "message_index.json"

    def _ensure_dir(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[dict]:
        if not self._index_path.exists():
            return []
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("messages", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self._ensure_dir()
        tmp = self._index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"messages": entries}, f, ensure_ascii=False)
        tmp.replace(self._index_path)

    def store(
        self,
        session_key: str,
        chat_id: str,
        text: str,
        timestamp: datetime | None = None,
    ) -> None:
        entries = self.load_all()
        ts = (timestamp or datetime.now(timezone.utc)).isoformat()
        entry = {
            "ts": ts,
            "chat_id": chat_id,
            "session_key": session_key,
            "text_hash": _text_hash(text),
        }
        entries.append(entry)
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries:]
        self._save(entries)

    def lookup_by_timestamp(
        self,
        target_time: datetime,
        tolerance_seconds: float = 5.0,
    ) -> dict | None:
        entries = self.load_all()
        target_ts = target_time.timestamp()
        for entry in reversed(entries):
            try:
                entry_ts = datetime.fromisoformat(entry["ts"]).timestamp()
            except (ValueError, KeyError):
                continue
            if abs(entry_ts - target_ts) <= tolerance_seconds:
                return entry
        return None

    def lookup_by_text_hash(self, text: str) -> dict | None:
        entries = self.load_all()
        target_hash = _text_hash(text)
        for entry in reversed(entries):
            if entry.get("text_hash") == target_hash:
                return entry
        return None


def content_search_sessions(workspace_path: Path, text: str) -> str | None:
    """Search all session files for a message matching the given text.

    Returns the session_key if found, None otherwise.
    """
    sessions_dir = Path(workspace_path) / "sessions"
    if not sessions_dir.exists():
        return None

    target_hash = _text_hash(text)

    for session_file in sessions_dir.glob("telegram_*.jsonl"):
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                session_key = None
                for line in f:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("_type") == "metadata":
                        session_key = msg.get("key")
                        continue
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                        if _text_hash(content) == target_hash:
                            return session_key
        except OSError:
            continue
    return None


@dataclass
class _StreamBuf:
    """Per-chat streaming accumulator for progressive message editing."""
    text: str = ""
    message_id: int | None = None
    last_edit: float = 0.0
    stream_id: str | None = None


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    proxy: str | None = None
    reply_to_message: bool = False
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    connection_pool_size: int = 32
    pool_timeout: float = 5.0
    streaming: bool = True
    # Enable inline keyboard buttons in Telegram messages.
    inline_keyboards: bool = False
    # Prevent bot-to-bot reply loops: if a bot replies to our message, ignore it.
    bot2bot_loop_prevention: bool = True
    stream_edit_interval: float = Field(default=_STREAM_EDIT_INTERVAL_DEFAULT, ge=0.1)
    admin_users: list[str] = Field(default_factory=list)
    group_allow_all: bool = False


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"
    display_name = "Telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("restart", "Restart the bot"),
        BotCommand("status", "Show bot status"),
        BotCommand("history", "Show recent conversation messages"),
        BotCommand("goal", "Start a sustained objective (long-running task)"),
        BotCommand("pairing", "Manage DM pairing (approve/deny/list)"),
        BotCommand("model", "Switch runtime model preset"),
        BotCommand("dream", "Run Dream memory consolidation now"),
        BotCommand("dream_log", "Show the latest Dream memory change"),
        BotCommand("dream_restore", "Restore Dream memory to an earlier version"),
        BotCommand("addgroup", "Add group to allowlist (admin only)"),
        BotCommand("removegroup", "Remove group from allowlist (admin only)"),
        BotCommand("groups", "List allowed and seen groups (admin only)"),
        BotCommand("help", "Show available commands"),
    ]

    # Regex for slash commands routed to AgentLoop via ``_forward_command``.
    # Hyphenated ``dream-*`` commands stay on a separate handler (below).
    TELEGRAM_BUS_SLASH_COMMAND_RE = re.compile(
        r"^/(?:new|stop|restart|status|dream|history|goal|pairing|model)(?:@\w+)?(?:\s+.*)?$"
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TelegramConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TelegramConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._stream_bufs: dict[str, _StreamBuf] = {}  # chat_id -> streaming state
        self._runtime_groups: set[str] = set(self.config.group_allow_from or [])
        self._policy_overrides: dict[str, str] = {}  # group_id -> policy ("respond_all" or "mention")

    def is_allowed(self, sender_id: str, *, is_dm: bool = False) -> bool:
        """Preserve Telegram's legacy id|username allowlist matching.

        When is_dm=True and group_allow_all is enabled, also allows users
        who have been seen in any authorized group.
        """
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])

        sender_str = str(sender_id)
        user_id: int | None = None

        if sender_str.count("|") == 1:
            sid, username = sender_str.split("|", 1)
            if sid.isdigit() and username:
                if sid in allow_list or username in allow_list:
                    return True
                user_id = int(sid)
        elif sender_str.isdigit():
            if sender_str in allow_list:
                return True
            user_id = int(sender_str)

        if is_dm and self.config.group_allow_all and user_id is not None:
            return self._is_user_in_authorized_group(user_id)

        return False

    def _is_user_in_authorized_group(self, user_id: int) -> bool:
        """Check if user_id exists in any authorized group's member list."""
        members = load_group_members()
        for group_id, member_list in members.items():
            if group_id in self._runtime_groups and user_id in member_list:
                return True
        return False

    def _track_group_member(self, group_id: str, user_id: int) -> None:
        """Add user_id to the group's member list if not already present."""
        members = load_group_members()
        if group_id not in members:
            members[group_id] = []
        if user_id not in members[group_id]:
            members[group_id].append(user_id)
            save_group_members(members)

    def _remove_group_member(self, group_id: str, user_id: int) -> None:
        """Remove user_id from the group's member list."""
        members = load_group_members()
        if group_id in members and user_id in members[group_id]:
            members[group_id].remove(user_id)
            save_group_members(members)
            self.logger.debug("Removed user {} from group {} member list", user_id, group_id)

    def is_admin(self, sender_id: str) -> bool:
        """Check if sender is in the admin_users list."""
        admin_list = self.config.admin_users
        if not admin_list:
            return False

        sender_str = str(sender_id)
        if sender_str in admin_list:
            return True

        if sender_str.count("|") == 1:
            sid, username = sender_str.split("|", 1)
            return sid in admin_list or username in admin_list

        return False

    def _track_seen_group(self, chat_id: int, chat_title: str | None) -> None:
        """Track a group chat as seen for the /groups command."""
        data = _load_groups_data()
        _prune_old_seen_groups(data)

        chat_id_str = str(chat_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Handle both string format and dict format for allowed groups
        allowed = data.get("allowed", [])
        allowed_ids = {
            str(g) if isinstance(g, str) else str(g.get("id", ""))
            for g in allowed
        }
        if chat_id_str in allowed_ids:
            return

        seen_list = data.get("seen", [])
        for entry in seen_list:
            if str(entry.get("id")) == chat_id_str:
                entry["last_seen"] = now_iso
                if chat_title:
                    entry["name"] = chat_title
                _save_groups_data(data)
                return

        seen_list.append({
            "id": chat_id_str,
            "name": chat_title or "Unknown",
            "last_seen": now_iso,
        })
        data["seen"] = seen_list
        _save_groups_data(data)

    @staticmethod
    def _normalize_telegram_command(content: str) -> str:
        """Map Telegram-safe command aliases back to canonical nanobot commands."""
        if not content.startswith("/"):
            return content
        if content == "/dream_log" or content.startswith("/dream_log "):
            return content.replace("/dream_log", "/dream-log", 1)
        if content == "/dream_restore" or content.startswith("/dream_restore "):
            return content.replace("/dream_restore", "/dream-restore", 1)
        return content

    def _load_and_merge_groups(self) -> None:
        """Load persisted groups and merge with config at runtime."""
        persisted = _load_persisted_groups()
        config_groups = self.config.group_allow_from or []
        self._runtime_groups = set(config_groups) | set(persisted)
        # Load policy overrides
        data = _load_groups_data()
        self._policy_overrides = data.get("policy_overrides", {})

    def _get_effective_groups(self) -> list[str]:
        """Return effective group allowlist (config + persisted, deduplicated)."""
        return sorted(self._runtime_groups)

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            self.logger.error("bot token not configured")
            return

        self._load_and_merge_groups()
        self._running = True

        proxy = self.config.proxy or None

        # Separate pools so long-polling (getUpdates) never starves outbound sends.
        api_request = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        poll_request = HTTPXRequest(
            connection_pool_size=4,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        builder = (
            Application.builder()
            .token(self.config.token)
            .request(api_request)
            .get_updates_request(poll_request)
        )
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add command handlers (using Regex to support @username suffixes before bot initialization)
        self._app.add_handler(MessageHandler(filters.Regex(r"^/start(?:@\w+)?$"), self._on_start))
        self._app.add_handler(
            MessageHandler(
                filters.Regex(TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE),
                self._forward_command,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.Regex(r"^/(dream-log|dream_log|dream-restore|dream_restore)(?:@\w+)?(?:\s+.*)?$"),
                self._forward_command,
            )
        )
        self._app.add_handler(MessageHandler(filters.Regex(r"^/help(?:@\w+)?$"), self._on_help))
        self._app.add_handler(MessageHandler(filters.Regex(r"^/groups(?:@\w+)?$"), self._on_groups))

        # Admin-only group management commands
        self._app.add_handler(
            MessageHandler(filters.Regex(r"^/addgroup(?:@\w+)?(?:\s+.*)?$"), self._on_addgroup)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(r"^/removegroup(?:@\w+)?(?:\s+.*)?$"), self._on_removegroup)
        )

        # Add message handler for text, photos, video, voice, documents, and locations
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
                 | filters.ANIMATION | filters.VOICE | filters.AUDIO
                 | filters.Document.ALL | filters.LOCATION)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        # Handler for bot joining/leaving groups (my_chat_member updates)
        self._app.add_handler(ChatMemberHandler(self._on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

        # Handler for other users joining/leaving groups (for privacy boundary detection)
        self._app.add_handler(ChatMemberHandler(self._on_chat_member, ChatMemberHandler.CHAT_MEMBER))

        # Always register callback handler for group approval buttons (and optionally user keyboards)
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query))
        allowed_updates = ["message", "callback_query", "my_chat_member", "chat_member"]
        if self.config.inline_keyboards:
            self.logger.debug("inline keyboards enabled")

        self.logger.info("Starting bot (polling mode)...")

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        self.logger.info("bot @{} connected", bot_info.username)

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            self.logger.debug("bot commands registered")
        except Exception as e:
            self.logger.warning("Failed to register bot commands: {}", e)

        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=allowed_updates,
            drop_pending_updates=False,  # Process pending messages on startup
            error_callback=self._on_polling_error,
        )

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        if self._app:
            self.logger.info("Stopping bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext in ("mp4", "mov", "avi", "mkv", "webm", "3gp"):
            return "video"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    @staticmethod
    def _is_remote_media_url(path: str) -> bool:
        return path.startswith(("http://", "https://"))

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            self.logger.warning("bot not running")
            return

        # Only stop typing indicator and remove reaction for final responses
        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)
            if reply_to_message_id := msg.metadata.get("message_id"):
                with suppress(ValueError):
                    await self._remove_reaction(msg.chat_id, int(reply_to_message_id))

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            self.logger.exception("Invalid chat_id: {}", msg.chat_id)
            return
        reply_to_message_id = msg.metadata.get("message_id")
        message_thread_id = msg.metadata.get("message_thread_id")
        if message_thread_id is None and reply_to_message_id is not None:
            message_thread_id = self._message_threads.get((msg.chat_id, reply_to_message_id))
        thread_kwargs = {}
        if message_thread_id is not None:
            thread_kwargs["message_thread_id"] = message_thread_id

        reply_params = None
        if self.config.reply_to_message:
            if reply_to_message_id:
                reply_params = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True
                )

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": self._app.bot.send_photo,
                    "video": self._app.bot.send_video,
                    "voice": self._app.bot.send_voice,
                    "audio": self._app.bot.send_audio,
                }.get(media_type, self._app.bot.send_document)
                param = {
                    "photo": "photo",
                    "video": "video",
                    "voice": "voice",
                    "audio": "audio",
                }.get(media_type, "document")
                extra: dict[str, Any] = {}
                if media_type == "video":
                    extra["supports_streaming"] = True

                # Telegram Bot API accepts HTTP(S) URLs directly for media params.
                if self._is_remote_media_url(media_path):
                    ok, error = validate_url_target(media_path)
                    if not ok:
                        raise ValueError(f"unsafe media URL: {error}")
                    await self._call_with_retry(
                        sender,
                        chat_id=chat_id,
                        **{param: media_path},
                        reply_parameters=reply_params,
                        **thread_kwargs,
                        **extra,
                    )
                    continue

                media_bytes = Path(media_path).read_bytes()
                filename = Path(media_path).name
                send_kwargs = {param: media_bytes, "filename": filename}
                await self._call_with_retry(
                    sender,
                    chat_id=chat_id,
                    reply_parameters=reply_params,
                    **thread_kwargs,
                    **extra,
                    **send_kwargs,
                )
            except Exception:
                filename = media_path.rsplit("/", 1)[-1]
                self.logger.exception("Failed to send media {}", media_path)
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"[Failed to send: {filename}]",
                    reply_parameters=reply_params,
                    **thread_kwargs,
                )

        # Send text content
        if msg.content and msg.content != "[empty message]":
            render_as_blockquote = bool(msg.metadata.get("_tool_hint"))
            buttons = getattr(msg, "buttons", None) or []
            reply_markup = self._build_keyboard(buttons) if buttons else None
            text = msg.content
            # Fallback: no native keyboard → splice labels into the message so the choices survive.
            if buttons and reply_markup is None:
                text = f"{text}\n\n{self._buttons_as_text(buttons)}"
            chunks = split_message(text, TELEGRAM_MAX_MESSAGE_LEN)
            for i, chunk in enumerate(chunks):
                is_last = (i == len(chunks) - 1)
                await self._send_text(
                    chat_id, chunk, reply_params, thread_kwargs,
                    render_as_blockquote=render_as_blockquote,
                    reply_markup=reply_markup if is_last else None,
                )

    async def _call_with_retry(self, fn, *args, **kwargs):
        """Call an async Telegram API function with retry on pool/network timeout and RetryAfter."""
        from telegram.error import RetryAfter

        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except TimedOut:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = _SEND_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                self.logger.warning(
                    "timeout (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            except RetryAfter as e:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = float(e.retry_after)
                self.logger.warning(
                    "Flood Control (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
        render_as_blockquote: bool = False,
        reply_markup=None,
    ) -> None:
        """Send a plain text message with HTML fallback."""
        try:
            html = _tool_hint_to_telegram_blockquote(text) if render_as_blockquote else _markdown_to_telegram_html(text)
            await self._call_with_retry(
                self._app.bot.send_message,
                chat_id=chat_id, text=html, parse_mode="HTML",
                reply_parameters=reply_params,
                reply_markup=reply_markup,
                **(thread_kwargs or {}),
            )
        except BadRequest as e:
            self.logger.warning("HTML parse failed, falling back to plain text: {}", e)
            try:
                await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    reply_markup=reply_markup,
                    **(thread_kwargs or {}),
                )
            except Exception:
                self.logger.exception("Error sending message")
                raise

    @staticmethod
    def _is_not_modified_error(exc: Exception) -> bool:
        return isinstance(exc, BadRequest) and "message is not modified" in str(exc).lower()

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Progressive message editing: send on first delta, edit on subsequent ones."""
        if not self._app:
            return
        meta = metadata or {}
        int_chat_id = int(chat_id)
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_end"):
            buf = self._stream_bufs.get(chat_id)
            if not buf or not buf.message_id or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            self._stop_typing(chat_id)
            if reply_to_message_id := meta.get("message_id"):
                with suppress(ValueError):
                    await self._remove_reaction(chat_id, int(reply_to_message_id))
            thread_kwargs = {}
            if message_thread_id := meta.get("message_thread_id"):
                thread_kwargs["message_thread_id"] = message_thread_id
            raw_text = buf.text
            html = _markdown_to_telegram_html(raw_text)
            if len(html) <= TELEGRAM_HTML_MAX_LEN:
                primary_html = html
                extra_html_chunks = []
            else:
                html_chunks = split_message(html, TELEGRAM_HTML_MAX_LEN)
                primary_html = html_chunks[0]
                extra_html_chunks = html_chunks[1:]
            try:
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=primary_html, parse_mode="HTML",
                )
            except BadRequest as e:
                # Only fall back to plain text on actual HTML parse/format errors.
                # Network errors (TimedOut, NetworkError) should propagate immediately
                # to avoid doubling connection demand during pool exhaustion.
                if self._is_not_modified_error(e):
                    self.logger.debug("Final stream edit already applied for {}", chat_id)
                    self._stream_bufs.pop(chat_id, None)
                    return
                self.logger.debug("Final stream edit failed (HTML), trying plain: {}", e)
                # Fall back to raw markdown (not HTML) so users don't see raw tags.
                primary_plain = split_message(raw_text, TELEGRAM_MAX_MESSAGE_LEN)[0] if len(raw_text) > TELEGRAM_MAX_MESSAGE_LEN else raw_text
                try:
                    await self._call_with_retry(
                        self._app.bot.edit_message_text,
                        chat_id=int_chat_id, message_id=buf.message_id,
                        text=primary_plain,
                    )
                except Exception as e2:
                    if self._is_not_modified_error(e2):
                        self.logger.debug("Final stream plain edit already applied for {}", chat_id)
                    else:
                        self.logger.warning("Final stream edit failed: {}", e2)
                        raise  # Let ChannelManager handle retry
            for extra_html_chunk in extra_html_chunks:
                try:
                    await self._call_with_retry(
                        self._app.bot.send_message,
                        chat_id=int_chat_id, text=extra_html_chunk,
                        parse_mode="HTML",
                        **thread_kwargs,
                    )
                except Exception:
                    # Fall back to _send_text which handles HTML→plain gracefully.
                    await self._send_text(int_chat_id, extra_html_chunk)
            self._store_message_index(chat_id, raw_text)
            self._stream_bufs.pop(chat_id, None)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id
        buf.text += delta

        if not buf.text.strip():
            return

        now = time.monotonic()
        thread_kwargs = {}
        if message_thread_id := meta.get("message_thread_id"):
            thread_kwargs["message_thread_id"] = message_thread_id
        if buf.message_id is None:
            preview = _strip_md_block(buf.text)
            try:
                sent = await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=int_chat_id, text=preview,
                    **thread_kwargs,
                )
                buf.message_id = sent.message_id
                buf.last_edit = now
            except Exception as e:
                self.logger.warning("Stream initial send failed: {}", e)
                raise  # Let ChannelManager handle retry
        elif (now - buf.last_edit) >= self.config.stream_edit_interval:
            if len(buf.text) > TELEGRAM_MAX_MESSAGE_LEN:
                await self._flush_stream_overflow(int_chat_id, buf, thread_kwargs)
                buf.last_edit = now
                return
            preview = _strip_md_block(buf.text)
            try:
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=preview,
                )
                buf.last_edit = now
            except Exception as e:
                if self._is_not_modified_error(e):
                    buf.last_edit = now
                    return
                self.logger.warning("Stream edit failed: {}", e)
                raise  # Let ChannelManager handle retry

    async def _flush_stream_overflow(
        self,
        chat_id: int,
        buf: "_StreamBuf",
        thread_kwargs: dict,
    ) -> None:
        """Split an oversized stream buffer mid-flight.

        Edits the current stream message with the first chunk, sends any
        intermediate chunks as standalone messages, then opens a new message
        for the tail so subsequent deltas continue streaming into it.
        """
        chunks = split_message(buf.text, TELEGRAM_MAX_MESSAGE_LEN)
        if len(chunks) <= 1:
            return
        try:
            await self._call_with_retry(
                self._app.bot.edit_message_text,
                chat_id=chat_id, message_id=buf.message_id,
                text=chunks[0],
            )
        except Exception as e:
            if not self._is_not_modified_error(e):
                self.logger.warning("Stream overflow edit failed: {}", e)
                raise
        for chunk in chunks[1:-1]:
            await self._call_with_retry(
                self._app.bot.send_message,
                chat_id=chat_id, text=chunk, **thread_kwargs,
            )
        tail = chunks[-1]
        sent = await self._call_with_retry(
            self._app.bot.send_message,
            chat_id=chat_id, text=tail, **thread_kwargs,
        )
        buf.message_id = sent.message_id
        buf.text = tail

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        message = update.message
        is_dm = message.chat.type == "private"
        str_chat_id = str(message.chat_id)
        is_authorized_group = not is_dm and str_chat_id in self._runtime_groups

        if self.config.group_allow_all and is_authorized_group:
            self._track_group_member(str_chat_id, user.id)
        elif not self.is_allowed(self._sender_id(user), is_dm=is_dm):
            return

        await message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command for allowed users only."""
        if not update.message or not update.effective_user:
            return
        message = update.message
        user = update.effective_user
        is_dm = message.chat.type == "private"
        str_chat_id = str(message.chat_id)
        is_authorized_group = not is_dm and str_chat_id in self._runtime_groups

        if self.config.group_allow_all and is_authorized_group:
            self._track_group_member(str_chat_id, user.id)
        elif not self.is_allowed(self._sender_id(user), is_dm=is_dm):
            return

        await message.reply_text(build_help_text())

    async def _on_addgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /addgroup command to add a group to the allowlist (admin only).

        Syntax:
            /addgroup <group_id> [policy]

        Where policy is optional and can be:
            respond_all - respond to all messages without @mention
            mention - require @mention even in small groups
        """
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user)
        if not self.is_allowed(sender_id):
            return
        if not self.is_admin(sender_id):
            await update.message.reply_text("This command is admin-only.")
            return

        text = update.message.text or ""
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        # Handle @bot suffix in command
        if "@" in arg.split()[0] if arg else False:
            arg = " ".join(arg.split()[1:]) if len(arg.split()) > 1 else ""

        # Parse group_id and optional policy
        arg_parts = arg.split()
        if not arg_parts:
            await update.message.reply_text(
                "Usage: /addgroup <group_id> [policy]\n"
                "Example: /addgroup -100123456789\n"
                "         /addgroup -100123456789 respond_all\n"
                "         /addgroup -100123456789 mention"
            )
            return

        group_id = _parse_group_id(arg_parts[0])
        if not group_id:
            await update.message.reply_text(
                "Usage: /addgroup <group_id> [policy]\n"
                "Example: /addgroup -100123456789"
            )
            return

        policy: str | None = None
        if len(arg_parts) > 1:
            policy = arg_parts[1].lower()
            if policy not in ("respond_all", "mention"):
                await update.message.reply_text(
                    f"Invalid policy '{arg_parts[1]}'. Valid options: respond_all, mention"
                )
                return

        if group_id in self._runtime_groups:
            await update.message.reply_text(f"Group {group_id} is already in the allowlist.")
            return

        # Add to persisted groups
        persisted = _load_persisted_groups()
        if group_id not in persisted:
            persisted.append(group_id)
            _save_persisted_groups(persisted)

        # Save policy override if specified
        if policy:
            data = _load_groups_data()
            if "policy_overrides" not in data:
                data["policy_overrides"] = {}
            data["policy_overrides"][group_id] = policy
            _save_groups_data(data)
            self._policy_overrides[group_id] = policy

        self._runtime_groups.add(group_id)
        policy_note = f" with {policy} policy" if policy else ""
        await update.message.reply_text(f"Added group {group_id} to the allowlist{policy_note}.")
        self.logger.info("Admin {} added group {} to allowlist (policy={})", sender_id, group_id, policy)

    async def _on_removegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /removegroup command to remove a group from the allowlist (admin only)."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user)
        if not self.is_allowed(sender_id):
            return
        if not self.is_admin(sender_id):
            await update.message.reply_text("This command is admin-only.")
            return

        text = update.message.text or ""
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if "@" in arg.split()[0] if arg else False:
            arg = " ".join(arg.split()[1:]) if len(arg.split()) > 1 else ""

        group_id = _parse_group_id(arg)
        if not group_id:
            await update.message.reply_text(
                "Usage: /removegroup <group_id>\n"
                "Example: /removegroup -100123456789"
            )
            return

        config_groups = set(self.config.group_allow_from or [])
        if group_id in config_groups:
            await update.message.reply_text(
                f"Group {group_id} is defined in config and cannot be removed dynamically.\n"
                "Edit the config file to remove it."
            )
            return

        if group_id not in self._runtime_groups:
            await update.message.reply_text(f"Group {group_id} is not in the allowlist.")
            return

        persisted = _load_persisted_groups()
        if group_id in persisted:
            persisted.remove(group_id)
            _save_persisted_groups(persisted)

        # Remove policy override if present
        if group_id in self._policy_overrides:
            data = _load_groups_data()
            if "policy_overrides" in data and group_id in data["policy_overrides"]:
                del data["policy_overrides"][group_id]
                _save_groups_data(data)
            del self._policy_overrides[group_id]

        self._runtime_groups.discard(group_id)
        await update.message.reply_text(f"Removed group {group_id} from the allowlist.")
        self.logger.info("Admin {} removed group {} from allowlist", sender_id, group_id)

    async def _on_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /groups command - list allowed and seen groups (admin only)."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user)
        if not self.is_allowed(sender_id):
            return
        if not self.is_admin(sender_id):
            await update.message.reply_text("This command is admin-only.")
            return

        data = _load_groups_data()
        _prune_old_seen_groups(data)

        config_groups = set(self.config.group_allow_from or [])
        lines: list[str] = []

        if self._runtime_groups:
            lines.append("Allowed groups:")
            for gid in sorted(self._runtime_groups):
                source = "config" if gid in config_groups else "persisted"
                policy = self._policy_overrides.get(gid)
                policy_part = f", policy={policy}" if policy else ""
                try:
                    chat_id_int = int(gid)
                    name, member_count = await self._get_chat_info_by_id(chat_id_int)
                    name_part = f" — {name}" if name else ""
                    members_part = f", {member_count} members" if member_count else ""
                    lines.append(f"  {gid}{name_part} ({source}{members_part}{policy_part})")
                except ValueError:
                    lines.append(f"  {gid} ({source}{policy_part})")
        else:
            lines.append("Allowed groups: (none)")

        seen = data.get("seen", [])
        seen_not_allowed = [g for g in seen if g.get("id") not in self._runtime_groups]
        if seen_not_allowed:
            lines.append("")
            lines.append("Seen but not allowed:")
            for g in seen_not_allowed:
                gid = g.get("id", "?")
                name = g.get("name", "Unknown")
                last_seen = g.get("last_seen", "")
                relative = _format_relative_time(last_seen) if last_seen else "unknown"
                try:
                    chat_id_int = int(gid)
                    _, member_count = await self._get_chat_info_by_id(chat_id_int)
                    members_part = f", {member_count} members" if member_count else ""
                    lines.append(f"  {gid} — {name} (seen {relative}{members_part})")
                except ValueError:
                    lines.append(f"  {gid} — {name} (seen {relative})")

        await update.message.reply_text("\n".join(lines))

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    @staticmethod
    def _derive_topic_session_key(message) -> str | None:
        """Derive topic-scoped session key for Telegram chats with threads."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{message_thread_id}"

    @staticmethod
    def _build_message_metadata(message, user) -> dict:
        """Build common Telegram inbound metadata payload."""
        reply_to = getattr(message, "reply_to_message", None)
        return {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "chat_title": getattr(message.chat, "title", None),
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
            "reply_to_message_id": getattr(reply_to, "message_id", None) if reply_to else None,
        }

    async def _get_member_count(self, chat) -> int | None:
        """Get the member count for a chat (for privacy-aware context loading).

        Returns:
            Member count for groups, or 2 for private chats (user + bot).
            Returns None if unable to fetch.
        """
        if chat.type == "private":
            return 2  # Private DM = user + bot
        if not self._app:
            return None
        try:
            return await chat.get_member_count()
        except Exception as e:
            self.logger.debug("Could not get member count for {}: {}", chat.id, e)
            return None

    async def _get_chat_info_by_id(self, chat_id: int) -> tuple[str | None, int | None]:
        """Get chat name and member count by chat ID.

        Returns:
            Tuple of (chat_title, member_count). Either may be None on error.
        """
        if not self._app:
            return None, None
        try:
            chat = await self._app.bot.get_chat(chat_id)
            title = getattr(chat, "title", None)
            try:
                member_count = await chat.get_member_count()
            except Exception:
                member_count = None
            return title, member_count
        except Exception as e:
            self.logger.debug("Could not get chat info for {}: {}", chat_id, e)
            return None, None

    async def _extract_reply_context(self, message) -> str | None:
        """Extract text from the message being replied to, if any."""
        reply = getattr(message, "reply_to_message", None)
        if not reply:
            return None
        text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
        if len(text) > TELEGRAM_REPLY_CONTEXT_MAX_LEN:
            text = text[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."

        if not text:
            return None

        bot_id, _ = await self._ensure_bot_identity()
        reply_user = getattr(reply, "from_user", None)

        if bot_id and reply_user and getattr(reply_user, "id", None) == bot_id:
            return f"[Reply to bot: {text}]"
        elif reply_user and getattr(reply_user, "username", None):
            return f"[Reply to @{reply_user.username}: {text}]"
        elif reply_user and getattr(reply_user, "first_name", None):
            return f"[Reply to {reply_user.first_name}: {text}]"
        else:
            return f"[Reply to: {text}]"

    async def _download_message_media(
        self, msg, *, add_failure_content: bool = False
    ) -> tuple[list[str], list[str]]:
        """Download media from a message (current or reply). Returns (media_paths, content_parts)."""
        media_file = None
        media_type = None
        if getattr(msg, "photo", None):
            media_file = msg.photo[-1]
            media_type = "image"
        elif getattr(msg, "voice", None):
            media_file = msg.voice
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_file = msg.audio
            media_type = "audio"
        elif getattr(msg, "document", None):
            media_file = msg.document
            media_type = "file"
        elif getattr(msg, "video", None):
            media_file = msg.video
            media_type = "video"
        elif getattr(msg, "video_note", None):
            media_file = msg.video_note
            media_type = "video"
        elif getattr(msg, "animation", None):
            media_file = msg.animation
            media_type = "animation"
        if not media_file or not self._app:
            return [], []
        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(
                media_type,
                getattr(media_file, "mime_type", None),
                getattr(media_file, "file_name", None),
            )
            media_dir = get_media_dir("telegram")
            unique_id = getattr(media_file, "file_unique_id", media_file.file_id)
            file_path = media_dir / f"{unique_id}{ext}"
            await file.download_to_drive(str(file_path))
            path_str = str(file_path)
            if media_type in ("voice", "audio"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    self.logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                    return [path_str], [f"[transcription: {transcription}]"]
                return [path_str], [f"[{media_type}: {path_str}]"]
            return [path_str], [f"[{media_type}: {path_str}]"]
        except Exception as e:
            self.logger.warning("Failed to download message media: {}", e)
            if add_failure_content:
                return [], [f"[{media_type}: download failed]"]
            return [], []

    async def _ensure_bot_identity(self) -> tuple[int | None, str | None]:
        """Load bot identity once and reuse it for mention/reply checks."""
        if self._bot_user_id is not None or self._bot_username is not None:
            return self._bot_user_id, self._bot_username
        if not self._app:
            return None, None
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        return self._bot_user_id, self._bot_username

    @staticmethod
    def _has_mention_entity(
        text: str,
        entities,
        bot_username: str,
        bot_id: int | None,
    ) -> bool:
        """Check Telegram mention entities against the bot username."""
        handle = f"@{bot_username}".lower()
        for entity in entities or []:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if entity_type != "mention":
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            if text[offset : offset + length].lower() == handle:
                return True
        return handle in text.lower()

    async def _try_eval_capture(self, message) -> bool:
        """Detect and handle eval capture: reply to forwarded message in eval group.

        Returns True if this was an eval capture (message should not be processed normally).
        Adds 👀 reaction when starting, 👍 on success, removes reaction on failure.
        """
        from nanobot.config.loader import load_config

        config = load_config()
        if not config.eval.enabled or not config.eval.group_id:
            return False

        if str(message.chat_id) != config.eval.group_id:
            return False

        reply = getattr(message, "reply_to_message", None)
        if not reply:
            return False

        forward_origin = getattr(reply, "forward_origin", None)
        if not forward_origin:
            return False

        # Add eyes reaction to indicate processing started
        await self._add_reaction(str(message.chat_id), message.message_id, "👀")

        # Handle different forward_origin types (python-telegram-bot v22.7+)
        sender_user = getattr(forward_origin, "sender_user", None)
        sender_chat = getattr(forward_origin, "sender_chat", None)
        origin_chat = getattr(forward_origin, "chat", None)

        if sender_user:
            original_chat_id = str(sender_user.id)
        elif sender_chat:
            original_chat_id = str(sender_chat.id)
        elif origin_chat:
            original_chat_id = str(origin_chat.id)
        else:
            self.logger.info("Eval capture: forward has no identifiable source, skipping")
            await self._remove_reaction(str(message.chat_id), message.message_id)
            return False

        forward_date = forward_origin.date

        explanation = message.text or message.caption or ""
        if not explanation.strip():
            await self._remove_reaction(str(message.chat_id), message.message_id)
            return False

        success = await self._handle_eval_capture(
            original_chat_id=original_chat_id,
            forward_date=forward_date,
            bad_message_text=reply.text or reply.caption or "",
            explanation=explanation,
        )

        # Add thumbs up reaction on successful capture, remove eyes on failure
        if success:
            await self._add_reaction(str(message.chat_id), message.message_id, "👍")
        else:
            await self._remove_reaction(str(message.chat_id), message.message_id)

        return True

    def _store_message_index(self, chat_id: str, text: str) -> None:
        """Store sent message in the message index for eval capture lookup."""
        try:
            from nanobot.config.loader import load_config
            config = load_config()
            session_key = f"telegram:{chat_id}"
            index = MessageIndex(config.workspace_path)
            index.store(session_key, chat_id, text)
        except Exception as e:
            self.logger.debug("Failed to store message index: {}", e)

    async def _handle_eval_capture(
        self,
        original_chat_id: str,
        forward_date: datetime,
        bad_message_text: str,
        explanation: str,
    ) -> bool:
        """Extract context and store eval capture entry.

        Uses tiered session lookup:
        1. Direct lookup by original_chat_id
        2. Tier 1: Lookup by timestamp in message index
        3. Tier 2: Content search fallback across all sessions

        Returns True if eval was successfully captured, False otherwise.
        """
        from nanobot.config.loader import load_config
        from nanobot.session.manager import SessionManager

        config = load_config()
        session_key = f"telegram:{original_chat_id}"

        sm = SessionManager(config.workspace_path)
        session_data = sm.read_session_file(session_key)

        if not session_data:
            self.logger.debug("Eval capture: direct session lookup failed for {}, trying index", session_key)
            index = MessageIndex(config.workspace_path)
            index_entry = index.lookup_by_timestamp(forward_date, tolerance_seconds=5)
            if not index_entry:
                index_entry = index.lookup_by_text_hash(bad_message_text)
            if index_entry:
                session_key = index_entry["session_key"]
                original_chat_id = session_key.split(":", 1)[1] if ":" in session_key else original_chat_id
                session_data = sm.read_session_file(session_key)
                self.logger.debug("Eval capture: index lookup found session {}", session_key)

        if not session_data:
            self.logger.debug("Eval capture: index lookup failed, trying content search")
            found_key = content_search_sessions(config.workspace_path, bad_message_text)
            if found_key:
                session_key = found_key
                original_chat_id = session_key.split(":", 1)[1] if ":" in session_key else original_chat_id
                session_data = sm.read_session_file(session_key)
                self.logger.debug("Eval capture: content search found session {}", session_key)

        if not session_data:
            self.logger.info("Eval capture: no session found for {} (all tiers failed), skipping", original_chat_id)
            return False

        context: list[dict[str, str]] = []
        if session_data.get("messages"):
            messages = session_data["messages"]
            target_ts = forward_date.timestamp()
            matching_idx: int | None = None
            for i, msg in enumerate(messages):
                msg_ts_str = msg.get("timestamp")
                if not msg_ts_str:
                    continue
                try:
                    msg_ts = datetime.fromisoformat(msg_ts_str).timestamp()
                except ValueError:
                    continue
                if abs(msg_ts - target_ts) <= 30:
                    matching_idx = i
                    break

            if matching_idx is not None:
                start_idx = max(0, matching_idx - 3)
                for msg in messages[start_idx : matching_idx + 1]:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict)
                        )
                    if role in ("user", "assistant") and content:
                        context.append({"role": role, "content": content})

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "original_chat_id": original_chat_id,
            "original_timestamp": forward_date.isoformat(),
            "bad_message": bad_message_text,
            "context": context,
            "explanation": explanation,
            "session_file": f"telegram_{original_chat_id}.jsonl",
        }

        evals_dir = config.workspace_path / "evals"
        evals_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = evals_dir / "feedback.jsonl"

        with open(feedback_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self.logger.info("Captured eval feedback for chat {}", original_chat_id)
        return True

    async def _is_group_message_for_bot(self, message) -> bool:
        """Allow group messages when policy is open, @mentioned, or replying to the bot.

        Policy precedence:
        1. Per-group policy override (respond_all/mention) takes priority
        2. Otherwise, use global group_policy setting

        When group_policy is "open" and group is in allowed list:
        - If member_count <= 2 (1:1 chat): respond to all messages
        - If member_count > 2 (larger group): require @mention or reply to bot
        - If member_count unavailable: require @mention or reply (conservative fallback)
        """
        if message.chat.type == "private":
            return True

        group_id_str = str(message.chat_id)
        in_allowed_group = not self._runtime_groups or group_id_str in self._runtime_groups

        # Check per-group policy override first
        policy_override = self._policy_overrides.get(group_id_str)
        if policy_override == "respond_all":
            # respond_all: always respond to messages in this group
            return True
        if policy_override == "mention":
            # mention: always require @mention, skip member count logic
            bot_id, bot_username = await self._ensure_bot_identity()
            if bot_username:
                text = message.text or ""
                caption = message.caption or ""
                if self._has_mention_entity(
                    text,
                    getattr(message, "entities", None),
                    bot_username,
                    bot_id,
                ):
                    return True
                if self._has_mention_entity(
                    caption,
                    getattr(message, "caption_entities", None),
                    bot_username,
                    bot_id,
                ):
                    return True
            reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
            if bot_id and reply_user and reply_user.id == bot_id:
                return True
            return False

        # Smart open policy: auto-respond only in 1:1 groups (user + bot)
        if self.config.group_policy == "open" and in_allowed_group:
            member_count = await self._get_member_count(message.chat)
            if member_count is not None and member_count <= 2:
                return True
            # Fall through to mention check for larger groups or unknown count

        # Check if bot is @mentioned or replied to (always allowed, even from unlisted groups)
        bot_id, bot_username = await self._ensure_bot_identity()
        if bot_username:
            text = message.text or ""
            caption = message.caption or ""
            if self._has_mention_entity(
                text,
                getattr(message, "entities", None),
                bot_username,
                bot_id,
            ):
                return True
            if self._has_mention_entity(
                caption,
                getattr(message, "caption_entities", None),
                bot_username,
                bot_id,
            ):
                return True

        reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        if bot_id and reply_user and reply_user.id == bot_id:
            return True

        # Non-addressed messages in 1:1 allowed groups with open policy are already handled above.
        # This final check is for policy="mention" or non-allowed groups.
        return False

    def _remember_thread_context(self, message) -> None:
        """Cache Telegram thread context by chat/message id for follow-up replies."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return
        key = (str(message.chat_id), message.message_id)
        self._message_threads[key] = message_thread_id
        if len(self._message_threads) > 1000:
            self._message_threads.pop(next(iter(self._message_threads)))

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        message = update.message
        user = update.effective_user
        sender_id = self._sender_id(user)
        is_dm = message.chat.type == "private"
        str_chat_id = str(message.chat_id)
        is_authorized_group = not is_dm and str_chat_id in self._runtime_groups

        # For groups with group_allow_all and authorized group: skip allowFrom check
        skip_allowed_check = self.config.group_allow_all and is_authorized_group
        if skip_allowed_check:
            self._track_group_member(str_chat_id, user.id)
        elif not self.is_allowed(sender_id, is_dm=is_dm):
            return

        self._remember_thread_context(message)

        # Strip @bot_username suffix if present
        content = message.text or ""
        if content.startswith("/") and "@" in content:
            cmd_part, *rest = content.split(" ", 1)
            cmd_part = cmd_part.split("@")[0]
            content = f"{cmd_part} {rest[0]}" if rest else cmd_part
        content = self._normalize_telegram_command(content)

        # Build metadata with member_count for privacy-aware context loading
        metadata = self._build_message_metadata(message, user)
        metadata["member_count"] = await self._get_member_count(message.chat)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content=content,
            metadata=metadata,
            session_key=self._derive_topic_session_key(message),
            is_dm=message.chat.type == "private",
            skip_allowed_check=skip_allowed_check,
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)
        is_dm = message.chat.type == "private"
        str_chat_id = str(chat_id)
        is_authorized_group = not is_dm and str_chat_id in self._runtime_groups

        # For groups with group_allow_all and authorized group: skip allowFrom check
        skip_allowed_check = self.config.group_allow_all and is_authorized_group
        if skip_allowed_check:
            self._track_group_member(str_chat_id, user.id)
        elif not self.is_allowed(sender_id, is_dm=is_dm):
            return

        # Bot-to-bot loop prevention: if a bot replies to our message, ignore it.
        is_from_bot = user and getattr(user, "is_bot", False)
        if is_from_bot and self.config.bot2bot_loop_prevention:
            reply_to = getattr(message, "reply_to_message", None)
            if reply_to:
                bot_id, _ = await self._ensure_bot_identity()
                reply_from = getattr(reply_to, "from_user", None)
                if bot_id and reply_from and getattr(reply_from, "id", None) == bot_id:
                    text_preview = (message.text or message.caption or "")[:50]
                    self.logger.info("Ignoring bot reply to prevent loop: {}", text_preview)
                    return

        self._remember_thread_context(message)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Track seen groups for /groups command
        if not is_dm:
            self._track_seen_group(chat_id, getattr(message.chat, "title", None))

        # Eval capture: detect replies to forwarded messages in eval group
        if await self._try_eval_capture(message):
            return

        if not await self._is_group_message_for_bot(message):
            return

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Location content
        if message.location:
            lat = message.location.latitude
            lon = message.location.longitude
            content_parts.append(f"[location: {lat}, {lon}]")

        # Download current message media
        current_media_paths, current_media_parts = await self._download_message_media(
            message, add_failure_content=True
        )
        media_paths.extend(current_media_paths)
        content_parts.extend(current_media_parts)
        if current_media_paths:
            self.logger.debug("Downloaded message media to {}", current_media_paths[0])

        # Reply context: text and/or media from the replied-to message
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            reply_ctx = await self._extract_reply_context(message)
            reply_media, reply_media_parts = await self._download_message_media(reply)
            if reply_media:
                media_paths = reply_media + media_paths
                self.logger.debug("Attached replied-to media: {}", reply_media[0])
            tag = reply_ctx or (f"[Reply to: {reply_media_parts[0]}]" if reply_media_parts else None)
            if tag:
                content_parts.insert(0, tag)
        content = "\n".join(content_parts) if content_parts else "[empty message]"

        self.logger.debug("message from {}: {}...", sender_id, content[:50])

        str_chat_id = str(chat_id)
        metadata = self._build_message_metadata(message, user)
        metadata["member_count"] = await self._get_member_count(message.chat)
        session_key = self._derive_topic_session_key(message)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "contents": [], "media": [],
                    "metadata": metadata,
                    "session_key": session_key,
                    "skip_allowed_check": skip_allowed_check,
                }
                self._start_typing(str_chat_id)
                await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        # Start typing indicator before processing
        self._start_typing(str_chat_id)
        await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
            skip_allowed_check=skip_allowed_check,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"],
                session_key=buf.get("session_key"),
                skip_allowed_check=buf.get("skip_allowed_check", False),
            )
        finally:
            self._media_group_tasks.pop(key, None)

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _add_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        """Add emoji reaction to a message (best-effort, non-blocking)."""
        if not self._app or not emoji:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            self.logger.debug("reaction failed: {}", e)

    async def _remove_reaction(self, chat_id: str, message_id: int) -> None:
        """Remove emoji reaction from a message (best-effort, non-blocking)."""
        if not self._app:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[],
            )
        except Exception as e:
            self.logger.debug("reaction removal failed: {}", e)

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            with suppress(asyncio.CancelledError):
                while self._app:
                    await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                    await asyncio.sleep(4)
        except Exception as e:
            self.logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    @staticmethod
    def _format_telegram_error(exc: Exception) -> str:
        """Return a short, readable error summary for logs."""
        text = str(exc).strip()
        if text:
            return text
        if exc.__cause__ is not None:
            cause = exc.__cause__
            cause_text = str(cause).strip()
            if cause_text:
                return f"{exc.__class__.__name__} ({cause_text})"
            return f"{exc.__class__.__name__} ({cause.__class__.__name__})"
        return exc.__class__.__name__

    def _on_polling_error(self, exc: Exception) -> None:
        """Keep long-polling network failures to a single readable line."""
        summary = self._format_telegram_error(exc)
        if isinstance(exc, (NetworkError, TimedOut)):
            self.logger.warning("polling network issue: {}", summary)
        else:
            self.logger.error("polling error: {}", summary)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        summary = self._format_telegram_error(context.error)

        if isinstance(context.error, (NetworkError, TimedOut)):
            self.logger.warning("network issue: {}", summary)
        else:
            self.logger.error("error: {}", summary)

    def _get_extension(
        self,
        media_type: str,
        mime_type: str | None,
        filename: str | None = None,
    ) -> str:
        """Get file extension based on media type or original filename."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                "video/x-matroska": ".mkv", "video/3gpp": ".3gp",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "video": ".mp4", "file": ""}
        if ext := type_map.get(media_type, ""):
            return ext

        if filename:
            return "".join(Path(filename).suffixes)

        return ""

    def _build_keyboard(self, buttons: list) -> InlineKeyboardMarkup | None:
        """Build inline keyboard markup if inline_keyboards is enabled."""
        if not buttons or not self.config.inline_keyboards:
            return None
        keyboard = [
            [InlineKeyboardButton(label, callback_data=self._safe_callback_data(label)) for label in row]
            for row in buttons
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def _safe_callback_data(label: str) -> str:
        # Telegram caps callback_data at 64 bytes UTF-8; truncate at a char boundary so the keyboard still sends.
        encoded = label.encode("utf-8")
        if len(encoded) <= 64:
            return label
        return encoded[:64].decode("utf-8", errors="ignore")

    @staticmethod
    def _buttons_as_text(buttons: list[list[str]]) -> str:
        # Buttons are semantic options; when we can't render a keyboard, the user still needs to see them.
        return "\n".join(" ".join(f"[{label}]" for label in row) for row in buttons if row)

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button clicks (callback queries)."""
        if not update.callback_query or not update.effective_user:
            return
        query = update.callback_query
        user = update.effective_user
        chat_id = query.message.chat_id if query.message else None
        sender_id = self._sender_id(user)
        callback_data = query.data or ""

        # Handle group approval callbacks (format: "grp_approve:<group_id>:<auth_user_id>" or "grp_leave:...")
        if callback_data.startswith("grp_approve:") or callback_data.startswith("grp_leave:"):
            await self._handle_group_approval_callback(query, user)
            return

        if not chat_id:
            self.logger.warning("Callback query without chat_id")
            return
        if not self.is_allowed(sender_id):
            return
        button_label = callback_data
        await query.answer()
        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
        self.logger.debug("Inline button tap from {}: {}", sender_id, button_label)
        self._start_typing(str(chat_id))
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=button_label,
            metadata={
                "callback_query_id": query.id,
                "button_label": button_label,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_callback": True,
            },
        )

    async def _handle_group_approval_callback(self, query, user) -> None:
        """Handle [Approve] or [Leave] button clicks for pending group approval."""
        callback_data = query.data or ""
        parts = callback_data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid callback data", show_alert=True)
            return

        action, group_id_str, auth_user_id_str = parts

        # Verify the clicking user is the authorized recipient
        if str(user.id) != auth_user_id_str:
            await query.answer("Only the recipient can act on this request", show_alert=True)
            return

        # Load origins to check if group is still pending
        origins = load_group_origins()
        if group_id_str not in origins:
            await query.answer("Group not found (may have been removed)", show_alert=True)
            if query.message:
                with suppress(Exception):
                    await query.message.edit_reply_markup(reply_markup=None)
            return

        origin = origins[group_id_str]

        if action == "grp_approve":
            # Approve the group
            origin["approved"] = True
            origins[group_id_str] = origin
            save_group_origins(origins)

            # Also add to runtime groups allowlist
            if group_id_str not in self._runtime_groups:
                persisted = _load_persisted_groups()
                if group_id_str not in persisted:
                    persisted.append(group_id_str)
                    _save_persisted_groups(persisted)
                self._runtime_groups.add(group_id_str)

            await query.answer("Group approved!")
            self.logger.info("User {} approved group {}", user.id, group_id_str)
            if query.message:
                with suppress(Exception):
                    await query.message.edit_text(
                        f"{query.message.text}\n\n✅ Approved by @{user.username or user.id}"
                    )

        elif action == "grp_leave":
            # Leave the group and clean up
            try:
                await self._app.bot.leave_chat(int(group_id_str))
                self.logger.info("Left group {} on request from user {}", group_id_str, user.id)
            except Exception as e:
                self.logger.warning("Failed to leave group {}: {}", group_id_str, e)

            # Remove from group_origins
            del origins[group_id_str]
            save_group_origins(origins)

            # Remove from group_members
            members = load_group_members()
            if group_id_str in members:
                del members[group_id_str]
                save_group_members(members)

            # Remove from runtime groups if present
            self._runtime_groups.discard(group_id_str)
            persisted = _load_persisted_groups()
            if group_id_str in persisted:
                persisted.remove(group_id_str)
                _save_persisted_groups(persisted)

            await query.answer("Left the group")
            if query.message:
                with suppress(Exception):
                    await query.message.edit_text(
                        f"{query.message.text}\n\n❌ Left by request from @{user.username or user.id}"
                    )

    def _is_user_in_allow_from(self, user_id: int, username: str | None) -> bool:
        """Check if a user_id or username is in the allow_from list."""
        allow_list = self.config.allow_from
        if not allow_list:
            return False
        if "*" in allow_list:
            return True
        user_id_str = str(user_id)
        if user_id_str in allow_list:
            return True
        if username and username in allow_list:
            return True
        return False

    async def _on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle bot being added to or removed from a group."""
        if not update.my_chat_member:
            return

        chat_member = update.my_chat_member
        chat = chat_member.chat

        if chat.type == "private":
            return

        old_status = chat_member.old_chat_member.status if chat_member.old_chat_member else None
        new_status = chat_member.new_chat_member.status if chat_member.new_chat_member else None

        was_member = old_status in ("member", "administrator", "creator")
        is_member = new_status in ("member", "administrator", "creator")

        chat_id = chat.id
        chat_id_str = str(chat_id)
        chat_title = chat.title or "Unknown"

        if not was_member and is_member:
            # Bot was added to a group
            self._track_seen_group(chat_id, chat_title)
            self.logger.info("Bot added to group '{}' (ID: {})", chat_title, chat_id)

            # Determine if adder is in allow_from
            adder = chat_member.from_user
            adder_id = adder.id if adder else 0
            adder_username = adder.username if adder else None
            is_approved = self._is_user_in_allow_from(adder_id, adder_username)

            # Save to group_origins.json
            origins = load_group_origins()
            origins[chat_id_str] = {
                "added_by": adder_id,
                "added_at": time.time(),
                "approved": is_approved,
            }
            save_group_origins(origins)

            if is_approved:
                self.logger.info("Group {} auto-approved (adder {} in allowFrom)", chat_id, adder_id)
                # Notify admins about auto-approved group
                await self._notify_admins_group_join(chat_id, chat_title)
            else:
                self.logger.info("Group {} pending approval (adder {} not in allowFrom)", chat_id, adder_id)
                # Notify allowFrom users with approval buttons
                adder_display = f"@{adder_username}" if adder_username else f"user {adder_id}"
                await self._notify_allow_from_pending_group(chat_id, chat_title, adder_display)

        elif was_member and not is_member:
            # Bot was removed from a group
            self.logger.info("Bot removed from group '{}' (ID: {})", chat_title, chat_id)

            # Remove from group_origins.json
            origins = load_group_origins()
            if chat_id_str in origins:
                del origins[chat_id_str]
                save_group_origins(origins)
                self.logger.debug("Removed group {} from origins", chat_id)

            # Remove all members from this group in group_members.json
            members = load_group_members()
            if chat_id_str in members:
                del members[chat_id_str]
                save_group_members(members)
                self.logger.debug("Removed members for group {}", chat_id)

    async def _notify_admins_group_join(self, chat_id: int, chat_title: str) -> None:
        """Send DM to all configured admins when bot joins a new group (auto-approved)."""
        if not self._app:
            return

        admin_users = self.config.admin_users
        if not admin_users:
            self.logger.debug("No admin_users configured, skipping group join notification")
            return

        message = (
            f"Added to group '{chat_title}' (ID: {chat_id})\n"
            f"Use /addgroup {chat_id} to enable responses."
        )

        for admin_id in admin_users:
            try:
                await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=int(admin_id),
                    text=message,
                )
                self.logger.debug("Notified admin {} about group join", admin_id)
            except Exception as e:
                self.logger.warning("Failed to notify admin {}: {}", admin_id, e)

    async def _notify_allow_from_pending_group(
        self, chat_id: int, chat_title: str, adder_display: str
    ) -> None:
        """DM all allowFrom users with approval buttons when a pending group is added."""
        if not self._app:
            return

        allow_from = self.config.allow_from
        if not allow_from:
            self.logger.debug("No allow_from configured, skipping pending group notification")
            return

        # Skip wildcard allowFrom
        if "*" in allow_from:
            self.logger.debug("allow_from is wildcard, skipping pending group notification")
            return

        message = f"{adder_display} added me to group '{chat_title}'."

        for user_id_or_name in allow_from:
            # Only notify numeric user IDs (we can't DM by username)
            if not user_id_or_name.lstrip("-").isdigit():
                self.logger.debug("Skipping non-numeric allowFrom entry: {}", user_id_or_name)
                continue

            user_id = int(user_id_or_name)
            # Build callback data with authorized user ID for security
            # Format: action:group_id:authorized_user_id
            approve_data = f"grp_approve:{chat_id}:{user_id}"
            leave_data = f"grp_leave:{chat_id}:{user_id}"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Approve", callback_data=approve_data),
                    InlineKeyboardButton("Leave", callback_data=leave_data),
                ]
            ])

            try:
                await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=user_id,
                    text=message,
                    reply_markup=keyboard,
                )
                self.logger.debug("Notified allowFrom user {} about pending group {}", user_id, chat_id)
            except Exception as e:
                self.logger.warning("Failed to notify allowFrom user {}: {}", user_id, e)

    async def _on_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle other users joining/leaving a group.

        - When a new member joins a 1:1 group, notify admin about privacy boundary change.
        - When a member leaves/is kicked from an authorized group, remove them from
          group_members.json to potentially revoke their DM access.
        """
        if not update.chat_member:
            return

        chat_member = update.chat_member
        chat = chat_member.chat

        if chat.type == "private":
            return

        old_status = chat_member.old_chat_member.status if chat_member.old_chat_member else None
        new_status = chat_member.new_chat_member.status if chat_member.new_chat_member else None

        was_member = old_status in ("member", "administrator", "creator")
        is_member = new_status in ("member", "administrator", "creator")

        chat_id_str = str(chat.id)

        if not was_member and is_member:
            await self._check_privacy_boundary_change(chat)
        elif was_member and not is_member:
            user = chat_member.new_chat_member.user if chat_member.new_chat_member else None
            if user and chat_id_str in self._runtime_groups:
                self._remove_group_member(chat_id_str, user.id)
                self.logger.info(
                    "User {} left/kicked from authorized group {}, removed from member list",
                    user.id, chat_id_str
                )

    async def _check_privacy_boundary_change(self, chat) -> None:
        """Check if a group just crossed the privacy boundary (1:1 -> multi-user).

        When member count goes from 2 to 3+, DM admins that private context
        will no longer be loaded in that group.
        """
        if not self._app:
            return

        try:
            member_count = await chat.get_member_count()
        except Exception as e:
            self.logger.debug("Could not get member count for privacy check: {}", e)
            return

        # Privacy boundary: group just went from 1:1 (2 members) to multi-user (3+)
        # We detect this when member_count is exactly 3 (someone just joined a 2-person group)
        if member_count == 3:
            chat_title = chat.title or "Unknown"
            self.logger.info(
                "Group '{}' (ID: {}) now has {} members - private context disabled",
                chat_title, chat.id, member_count
            )
            await self._notify_privacy_boundary_change(chat.id, chat_title)

    async def _notify_privacy_boundary_change(self, chat_id: int, chat_title: str) -> None:
        """DM admins when a group crosses the privacy boundary."""
        if not self._app:
            return

        admin_users = self.config.admin_users
        if not admin_users:
            return

        message = (
            f"FYI: '{chat_title}' now has multiple members. "
            f"I'll keep private info out of that chat."
        )

        for admin_id in admin_users:
            try:
                await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=int(admin_id),
                    text=message,
                )
                self.logger.debug("Notified admin {} about privacy boundary change", admin_id)
            except Exception as e:
                self.logger.warning("Failed to notify admin {} about privacy change: {}", admin_id, e)
