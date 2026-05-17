"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from contextlib import suppress
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.utils.helpers import (
    current_time_str,
    detect_image_mime,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "SHARED.md", "TOOLS.md"]
    PRIVATE_FILE = "USER_PRIVATE.md"  # Only loaded when member_count <= 2
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(
        self,
        workspace: Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
        multi_user: bool = False,
    ):
        self.workspace = workspace
        self.timezone = timezone
        self.multi_user = multi_user
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        member_count: int | None = None,
        user_id: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills.

        Args:
            member_count: Number of members in the chat. When <= 2 (DM or 1:1 with bot),
                          USER_PRIVATE.md is included. When > 2, it's excluded for privacy.
            user_id: Channel user ID used to resolve per-user files in multi-user mode.
        """
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files(member_count=member_count, user_id=user_id)
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            history_text = "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            )
            history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
            parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(
        self,
        member_count: int | None = None,
        user_id: str | None = None,
    ) -> str:
        """Load all bootstrap files from workspace.

        Args:
            member_count: Number of members in the chat. USER_PRIVATE.md is only
                          loaded when member_count <= 2 (private/1:1 context).
            user_id: Channel user ID used to resolve per-user files in multi-user mode.
        """
        parts = []
        resolved_user_id = str(user_id).strip() if user_id is not None else ""

        for filename in self.BOOTSTRAP_FILES:
            actual_filename = self._resolve_bootstrap_filename(filename, resolved_user_id)
            if actual_filename is None:
                continue
            file_path = self.workspace / actual_filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {actual_filename}\n\n{content}")

        # Load USER_PRIVATE.md only in private contexts (DM or 1:1 with bot)
        # member_count None means unknown/CLI - default to private for safety
        private_filename = self._resolve_bootstrap_filename(self.PRIVATE_FILE, resolved_user_id)
        if private_filename is None:
            return "\n\n".join(parts) if parts else ""
        private_file = self.workspace / private_filename
        if private_file.exists():
            is_private_context = member_count is None or member_count <= 2
            if is_private_context:
                content = private_file.read_text(encoding="utf-8")
                parts.append(f"## {private_filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _resolve_bootstrap_filename(self, filename: str, resolved_user_id: str) -> str | None:
        """Resolve a bootstrap filename for single-user or multi-user loading."""
        if not self.multi_user:
            return filename

        if not resolved_user_id and filename in {"USER.md", self.PRIVATE_FILE}:
            return None

        if filename == "USER.md":
            return f"USER_{resolved_user_id}.md"
        if filename == self.PRIVATE_FILE:
            return f"USER_PRIVATE_{resolved_user_id}.md"
        return filename

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        with suppress(Exception):
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        member_count: int | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        extra = goal_state_runtime_lines(session_metadata)
        user_id = None
        if session_metadata and "user_id" in session_metadata:
            raw_user_id = session_metadata.get("user_id")
            if raw_user_id is not None:
                user_id = str(raw_user_id).strip() or None
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {"role": "system", "content": self.build_system_prompt(
                skill_names,
                channel=channel,
                session_summary=session_summary,
                member_count=member_count,
                user_id=user_id,
            )},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
