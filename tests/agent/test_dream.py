"""Tests for the Dream class — two-phase memory consolidation via AgentRunner."""

import json

import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.agent.memory import Dream, MemoryStore
from nanobot.agent.runner import AgentRunResult
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.utils.gitstore import LineAge


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_soul("# Soul\n- Helpful")
    s.write_user("# User\n- Developer")
    s.write_memory("# Memory\n- Project X active")
    return s


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def mock_runner():
    return MagicMock()


@pytest.fixture
def dream(store, mock_provider, mock_runner):
    d = Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)
    d._runner = mock_runner
    return d


def _make_run_result(
    stop_reason="completed",
    final_content=None,
    tool_events=None,
    usage=None,
):
    return AgentRunResult(
        final_content=final_content or stop_reason,
        stop_reason=stop_reason,
        messages=[],
        tools_used=[],
        usage={},
        tool_events=tool_events or [],
    )


class TestDreamRun:
    async def test_noop_when_no_unprocessed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should not call LLM when there's nothing to process."""
        result = await dream.run()
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()
        mock_runner.run.assert_not_called()

    async def test_calls_runner_for_unprocessed_entries(self, dream, mock_provider, mock_runner, store):
        """Dream should call AgentRunner when there are unprocessed history entries."""
        store.append_history("User prefers dark mode")
        mock_provider.chat_with_retry.return_value = MagicMock(content="New fact")
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "memory/MEMORY.md"}],
        ))
        result = await dream.run()
        assert result is True
        mock_runner.run.assert_called_once()
        spec = mock_runner.run.call_args[0][0]
        assert spec.max_iterations == 10
        assert spec.fail_on_tool_error is False

    async def test_advances_dream_cursor(self, dream, mock_provider, mock_runner, store):
        """Dream should advance the cursor after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        assert store.get_last_dream_cursor() == 2

    async def test_compacts_processed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should compact history after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        # After Dream, cursor is advanced and 3, compact keeps last max_history_entries
        entries = store.read_unprocessed_history(since_cursor=0)
        assert all(e["cursor"] > 0 for e in entries)

    async def test_skill_phase_uses_builtin_skill_creator_path(self, dream, mock_provider, mock_runner, store):
        """Dream should point skill creation guidance at the builtin skill-creator template."""
        store.append_history("Repeated workflow one")
        store.append_history("Repeated workflow two")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKILL] test-skill: test description")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        spec = mock_runner.run.call_args[0][0]
        system_prompt = spec.initial_messages[0]["content"]
        expected = str(BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md")
        assert expected in system_prompt

    async def test_skill_write_tool_accepts_workspace_relative_skill_path(self, dream, store):
        """Dream skill creation should allow skills/<name>/SKILL.md relative to workspace root."""
        write_tool = dream._tools.get("write_file")
        assert write_tool is not None

        result = await write_tool.execute(
            path="skills/test-skill/SKILL.md",
            content="---\nname: test-skill\ndescription: Test\n---\n",
        )

        assert "Successfully wrote" in result
        assert (store.workspace / "skills" / "test-skill" / "SKILL.md").exists()

    async def test_phase1_prompt_includes_line_age_annotations(self, dream, mock_provider, mock_runner, store):
        """Phase 1 prompt should have per-line age suffixes in MEMORY.md when git is available."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        # Init git so line_ages works
        store.git.init()
        store.git.auto_commit("initial memory state")

        await dream.run()

        # The MEMORY.md section should not crash and should contain the memory content
        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "## Current MEMORY.md" in user_msg

    async def test_phase1_annotates_only_memory_not_soul_or_user(self, dream, mock_provider, mock_runner, store):
        """SOUL.md and USER.md should never have age annotations — they are permanent."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        store.git.init()
        store.git.auto_commit("initial state")

        await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        # The ← suffix should only appear in MEMORY.md section
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        soul_section = user_msg.split("## Current SOUL.md")[1].split("## Current USER.md")[0]
        user_section = user_msg.split("## Current USER.md")[1]
        # SOUL and USER should not contain age arrows
        assert "\u2190" not in soul_section
        assert "\u2190" not in user_section

    async def test_phase1_prompt_works_without_git(self, dream, mock_provider, mock_runner, store):
        """Phase 1 should work fine even if git is not initialized (no age annotations)."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        # Should still succeed — just without age annotations
        mock_provider.chat_with_retry.assert_called_once()
        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "## Current MEMORY.md" in user_msg

    async def test_phase1_prompt_carries_age_suffix_for_stale_lines(
        self, dream, mock_provider, mock_runner, store,
    ):
        """End-to-end: ages >14d must appear verbatim in the LLM prompt, ages ≤14d must not."""
        # MEMORY.md fixture has 2 non-blank lines ("# Memory" and "- Project X active").
        # Inject four ages to cover threshold boundaries: >14 suffix, ==14 no suffix, <14 no suffix.
        store.write_memory("# Memory\n- Project X active\n- fresh item\n- edge case line")
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        fake_ages = [
            LineAge(age_days=30),   # "# Memory"        → should get ← 30d
            LineAge(age_days=20),   # "- Project X..."  → should get ← 20d
            LineAge(age_days=14),   # "- fresh item"    → ==14, threshold is strictly >14, no suffix
            LineAge(age_days=5),    # "- edge case..."  → no suffix
        ]
        with patch.object(store.git, "line_ages", return_value=fake_ages):
            await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        assert "\u2190 30d" in memory_section
        assert "\u2190 20d" in memory_section
        assert "\u2190 14d" not in memory_section
        assert "\u2190 5d" not in memory_section

    async def test_phase1_skips_annotation_when_disabled(
        self, dream, mock_provider, mock_runner, store,
    ):
        """`annotate_line_ages=False` must bypass the git lookup entirely and keep MEMORY.md raw."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        dream.annotate_line_ages = False
        # line_ages must be bypassed entirely — verify with a spy rather than a
        # raising side_effect, because _annotate_with_ages catches Exception
        # (which swallows AssertionError) and would hide an accidental call.
        with patch.object(store.git, "line_ages") as mock_line_ages:
            await dream.run()
            mock_line_ages.assert_not_called()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "\u2190" not in user_msg

    async def test_phase1_skips_annotation_on_line_ages_length_mismatch(
        self, dream, mock_provider, mock_runner, store,
    ):
        """If ages length != lines length (dirty working tree), skip annotation instead of mis-tagging."""
        # MEMORY.md has 2 non-blank lines but we hand back only 1 age → mismatch.
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        with patch.object(store.git, "line_ages", return_value=[LineAge(age_days=999)]):
            await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        # No age arrow at all — we refused to annotate rather than tag the wrong line.
        assert "\u2190" not in memory_section

    async def test_phase1_prompt_uses_threshold_from_template_var(
        self, dream, mock_provider, mock_runner, store,
    ):
        """System prompt should reference the stale-threshold constant, not a hardcoded 14."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        system_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][0]["content"]
        # The template renders with stale_threshold_days=14 → LLM must see "N>14"
        assert "N>14" in system_msg


class TestDreamPromptCaps:
    """Dream's Phase 1/2 prompt must not be poisoned by a legacy oversized
    history entry or a runaway MEMORY.md. Without caps, a single pre-#3412
    raw_archive dump in history.jsonl would make every subsequent Dream run
    exceed the context window and silently advance the cursor past real work.
    """

    async def test_phase1_caps_huge_memory_file(
        self, dream, mock_provider, mock_runner, store,
    ):
        """A MEMORY.md much larger than _MEMORY_FILE_MAX_CHARS must be truncated
        in the prompt preview (full content is still reachable via read_file)."""
        store.write_memory("M" * (dream._MEMORY_FILE_MAX_CHARS * 5))
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        user_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        assert len(memory_section) < dream._MEMORY_FILE_MAX_CHARS + 500

    async def test_phase1_caps_huge_history_entry(
        self, dream, mock_provider, mock_runner, store,
    ):
        """A legacy oversized history entry (e.g. pre-#3412 raw_archive dump)
        must not explode the Phase 1 prompt — each entry is capped in the
        preview, even though the JSONL record itself stays full-size."""
        # Bypass the append_history cap by writing directly, simulating a
        # record that was written by an older nanobot build before any caps.
        store.history_file.write_text(
            json.dumps({
                "cursor": 1,
                "timestamp": "2026-04-01 10:00",
                "content": "H" * (dream._HISTORY_ENTRY_PREVIEW_MAX_CHARS * 8),
            }) + "\n",
            encoding="utf-8",
        )
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        user_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        history_section = user_msg.split("## Conversation History\n")[1].split("\n\n## Current Date")[0]
        assert len(history_section) < dream._HISTORY_ENTRY_PREVIEW_MAX_CHARS + 500


class TestDreamUserMdStructure:
    """Tests for structured USER.md memory with Family table support."""

    async def test_dream_extracts_family_member_to_table(
        self, dream, mock_provider, mock_runner, store,
    ):
        """Dream should extract family member mentions and format for USER.md table."""
        store.write_user(
            "# User Profile\n\n## Family\n\n"
            "| Name | Relation | Birthday | Notes |\n"
            "|------|----------|----------|-------|\n"
        )
        store.append_history("User said: my daughter Emma turns 15 on October 1st")
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[USER] Family: Emma | daughter | October 1 | turns 15 in 2026"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "USER.md"}],
        ))

        await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "## Current USER.md" in user_msg
        assert "Family" in user_msg

    async def test_dream_extracts_preference_to_user_section(
        self, dream, mock_provider, mock_runner, store,
    ):
        """Dream should extract user preferences to the appropriate USER.md section."""
        store.write_user(
            "# User Profile\n\n## Preferences\n\n"
            "- **Food/Drink**: \n"
        )
        store.append_history("User mentioned: I prefer wine over beer")
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[USER] Preferences/Food/Drink: prefers wine"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "USER.md"}],
        ))

        await dream.run()

        mock_runner.run.assert_called_once()
        spec = mock_runner.run.call_args[0][0]
        phase2_user_msg = spec.initial_messages[1]["content"]
        assert "[USER]" in phase2_user_msg
        assert "wine" in phase2_user_msg

    async def test_dream_preserves_existing_user_content(
        self, dream, mock_provider, mock_runner, store,
    ):
        """Dream should preserve existing USER.md content when adding new facts."""
        existing_content = (
            "# User Profile\n\n"
            "## Identity\n\n"
            "- **Name**: John\n"
            "- **Timezone**: UTC-5\n\n"
            "## Family\n\n"
            "| Name | Relation | Birthday | Notes |\n"
            "|------|----------|----------|-------|\n"
            "| Sarah | wife | March 15 | |\n"
        )
        store.write_user(existing_content)
        store.append_history("User said: my son Jake's birthday is July 20")
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[USER] Family: Jake | son | July 20 |"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "USER.md"}],
        ))

        await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "John" in user_msg
        assert "Sarah" in user_msg
        assert "wife" in user_msg

    async def test_phase1_prompt_describes_user_md_sections(
        self, dream, mock_provider, mock_runner, store,
    ):
        """Phase 1 system prompt should describe USER.md structured sections."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        system_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][0]["content"]
        assert "Identity" in system_msg
        assert "Family" in system_msg
        assert "Health" in system_msg

    async def test_phase2_prompt_includes_family_table_instructions(
        self, dream, mock_provider, mock_runner, store,
    ):
        """Phase 2 system prompt should include instructions for editing Family table."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[USER] Family: Test | child | Jan 1 |"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        spec = mock_runner.run.call_args[0][0]
        system_msg = spec.initial_messages[0]["content"]
        assert "Family table" in system_msg
        assert "Name | Relation | Birthday | Notes" in system_msg


class TestDreamGroupSessionDetection:
    """Tests for group session detection logic."""

    def test_dm_session_not_detected_as_group(self, store, mock_provider):
        """DM sessions (positive Telegram IDs) should not be detected as groups."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        # Positive Telegram ID = DM
        assert dream.is_group_session("telegram:123456789") is False
        assert dream.is_group_session("telegram:987654321") is False

    def test_group_session_detected_by_negative_id(self, store, mock_provider):
        """Telegram groups (negative IDs) should be detected as groups."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        # Negative Telegram ID = group
        assert dream.is_group_session("telegram:-1001234567890") is True
        assert dream.is_group_session("telegram:-987654321") is True

    def test_topic_session_detected_as_group(self, store, mock_provider):
        """Telegram topic threads should be detected as groups."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        # Topic threads are always in groups
        assert dream.is_group_session("telegram:-1001234567890:topic:123") is True
        assert dream.is_group_session("telegram:123456789:topic:456") is True

    def test_member_count_metadata_overrides_pattern(self, store, mock_provider):
        """member_count >= 3 in metadata should mark as group regardless of key."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        # Even a DM-looking key with member_count >= 3 is a group
        assert dream.is_group_session("telegram:123456789", {"member_count": 3}) is True
        assert dream.is_group_session("telegram:123456789", {"member_count": 10}) is True

        # member_count <= 2 overrides pattern: negative ID with 2 members is a 1:1 chat
        assert dream.is_group_session("telegram:-1001234567890", {"member_count": 2}) is False
        assert dream.is_group_session("telegram:-1001234567890", {"member_count": 1}) is False

    def test_dm_with_low_member_count_not_group(self, store, mock_provider):
        """DM key with member_count <= 2 is not a group."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        assert dream.is_group_session("telegram:123456789", {"member_count": 1}) is False
        assert dream.is_group_session("telegram:123456789", {"member_count": 2}) is False

    def test_other_channels_not_detected_as_group_by_default(self, store, mock_provider):
        """Non-Telegram channels without group patterns are not groups by default."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        assert dream.is_group_session("discord:channel123") is False
        assert dream.is_group_session("slack:workspace:channel") is False
        assert dream.is_group_session("matrix:!roomid:server") is False


class TestDreamGroupSessionProcessing:
    """Tests for group session extraction to SHARED.md only."""

    @pytest.fixture
    def sessions_manager(self, tmp_path):
        """Create a SessionManager with test sessions."""
        from nanobot.session.manager import SessionManager
        return SessionManager(tmp_path)

    @pytest.fixture
    def dream_with_sessions(self, store, mock_provider, mock_runner, sessions_manager):
        """Dream instance with SessionManager for group processing."""
        d = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
            sessions=sessions_manager,
        )
        d._runner = mock_runner
        return d

    def _create_group_session(self, sessions_manager, session_key: str, messages: list[dict]):
        """Helper to create a test group session."""
        from nanobot.session.manager import Session
        session = sessions_manager.get_or_create(session_key)
        session.messages = messages
        session.metadata["member_count"] = 5  # Mark as group
        sessions_manager.save(session)
        return session

    async def test_group_session_extracts_to_shared_only(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager, store,
    ):
        """Group session extraction should only touch SHARED.md."""
        # Create a group session with messages
        session_key = "telegram:-1001234567890"
        self._create_group_session(sessions_manager, session_key, [
            {"role": "user", "content": "My daughter Emma's birthday is October 1st", "timestamp": "2026-01-15T10:00:00"},
            {"role": "assistant", "content": "I'll remember that!", "timestamp": "2026-01-15T10:00:05"},
        ])

        # Setup mocks
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[SHARED] Emma (daughter) birthday: October 1"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "SHARED.md"}],
        ))

        # Run session extraction
        result = await dream_with_sessions.run_session(session_key)

        assert result is True
        mock_provider.chat_with_retry.assert_called_once()

        # Verify Phase 1 uses group-specific prompt
        call_args = mock_provider.chat_with_retry.call_args
        system_msg = call_args.kwargs["messages"][0]["content"]
        assert "GROUP CONVERSATION" in system_msg or "SHARED.md" in system_msg

    async def test_group_session_skips_dm_sessions(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager,
    ):
        """DM sessions should be skipped by run_session."""
        # Create a DM session (positive ID)
        session_key = "telegram:123456789"
        self._create_group_session(sessions_manager, session_key, [
            {"role": "user", "content": "My secret plan", "timestamp": "2026-01-15T10:00:00"},
        ])
        # Override to make it a DM
        session = sessions_manager.get_or_create(session_key)
        session.metadata["member_count"] = 2
        sessions_manager.save(session)

        result = await dream_with_sessions.run_session(session_key)

        assert result is False
        mock_provider.chat_with_retry.assert_not_called()

    async def test_group_session_advances_cursor(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager, store,
    ):
        """Group session processing should advance per-session cursor."""
        session_key = "telegram:-1001234567890"
        self._create_group_session(sessions_manager, session_key, [
            {"role": "user", "content": "message 1", "timestamp": "2026-01-15T10:00:00"},
            {"role": "user", "content": "message 2", "timestamp": "2026-01-15T10:00:05"},
        ])

        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream_with_sessions.run_session(session_key)

        cursor = store.get_session_dream_cursor(session_key)
        assert cursor == 2

    async def test_run_sessions_processes_only_groups(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager, store,
    ):
        """run_sessions should only process group sessions, not DMs."""
        # Create one group and one DM session
        self._create_group_session(sessions_manager, "telegram:-1001234567890", [
            {"role": "user", "content": "group message", "timestamp": "2026-01-15T10:00:00"},
        ])

        dm_session = sessions_manager.get_or_create("telegram:123456789")
        dm_session.messages = [
            {"role": "user", "content": "private message", "timestamp": "2026-01-15T10:00:00"},
        ]
        dm_session.metadata["member_count"] = 2
        sessions_manager.save(dm_session)

        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        count = await dream_with_sessions.run_sessions()

        # Only the group session should be processed
        assert count == 1

    async def test_no_sessions_manager_returns_zero(self, store, mock_provider):
        """run_sessions without SessionManager should return 0."""
        dream = Dream(store=store, provider=mock_provider, model="test-model")

        count = await dream.run_sessions()

        assert count == 0

    async def test_retroactive_processing_of_existing_sessions(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager, store,
    ):
        """Existing group sessions (created before feature) should be processed retroactively."""
        session_key = "telegram:-1001234567890"
        # Simulate existing session with older messages
        self._create_group_session(sessions_manager, session_key, [
            {"role": "user", "content": "old msg 1", "timestamp": "2025-06-01T10:00:00"},
            {"role": "user", "content": "old msg 2", "timestamp": "2025-06-02T10:00:00"},
            {"role": "user", "content": "old msg 3", "timestamp": "2025-06-03T10:00:00"},
        ])

        # Cursor starts at 0 (no previous processing)
        assert store.get_session_dream_cursor(session_key) == 0

        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream_with_sessions.run_session(session_key)

        # All 3 messages should be processed, cursor should advance
        assert store.get_session_dream_cursor(session_key) == 3

    async def test_phase2_prompt_restricts_to_shared_only(
        self, dream_with_sessions, mock_provider, mock_runner, sessions_manager, store,
    ):
        """Phase 2 system prompt should explicitly restrict edits to SHARED.md."""
        session_key = "telegram:-1001234567890"
        self._create_group_session(sessions_manager, session_key, [
            {"role": "user", "content": "family info", "timestamp": "2026-01-15T10:00:00"},
        ])

        mock_provider.chat_with_retry.return_value = MagicMock(
            content="[SHARED] some family fact"
        )
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream_with_sessions.run_session(session_key)

        # Check Phase 2 system prompt
        spec = mock_runner.run.call_args[0][0]
        system_msg = spec.initial_messages[0]["content"]
        assert "SHARED.md" in system_msg
        assert "NEVER" in system_msg or "ONLY" in system_msg


class TestMemoryStoreSessionCursors:
    """Tests for per-session dream cursor tracking."""

    def test_get_session_cursor_default_zero(self, store):
        """New session cursors should default to 0."""
        cursor = store.get_session_dream_cursor("telegram:-1001234567890")
        assert cursor == 0

    def test_set_and_get_session_cursor(self, store):
        """Session cursors should persist correctly."""
        store.set_session_dream_cursor("telegram:-1001234567890", 5)
        cursor = store.get_session_dream_cursor("telegram:-1001234567890")
        assert cursor == 5

    def test_multiple_session_cursors_independent(self, store):
        """Different sessions should have independent cursors."""
        store.set_session_dream_cursor("telegram:-1001111111111", 3)
        store.set_session_dream_cursor("telegram:-1002222222222", 7)

        assert store.get_session_dream_cursor("telegram:-1001111111111") == 3
        assert store.get_session_dream_cursor("telegram:-1002222222222") == 7


class TestMemoryStoreSharedFile:
    """Tests for SHARED.md file operations."""

    def test_read_shared_empty_default(self, store):
        """read_shared should return empty string when file doesn't exist."""
        content = store.read_shared()
        assert content == ""

    def test_write_and_read_shared(self, store):
        """write_shared and read_shared should work correctly."""
        store.write_shared("# Shared\n- Family birthdays")
        content = store.read_shared()
        assert "Family birthdays" in content

    def test_read_user_private_empty_default(self, store):
        """read_user_private should return empty string when file doesn't exist."""
        content = store.read_user_private()
        assert content == ""

    def test_write_and_read_user_private(self, store):
        """write_user_private and read_user_private should work correctly."""
        store.write_user_private("# Private\n- Health info")
        content = store.read_user_private()
        assert "Health info" in content

