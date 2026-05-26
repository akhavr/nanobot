"""Tests for enhanced filesystem tools: ReadFileTool, EditFileTool, ListDirTool."""

import pytest

from nanobot.agent.tools.filesystem import (
    CopyFileTool,
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    _find_match,
)

# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------

class TestReadFileTool:

    @pytest.fixture()
    def tool(self, tmp_path):
        return ReadFileTool(workspace=tmp_path)

    @pytest.fixture()
    def sample_file(self, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8")
        return f

    @pytest.mark.asyncio
    async def test_basic_read_has_line_numbers(self, tool, sample_file):
        result = await tool.execute(path=str(sample_file))
        assert "1| line 1" in result
        assert "20| line 20" in result

    @pytest.mark.asyncio
    async def test_offset_and_limit(self, tool, sample_file):
        result = await tool.execute(path=str(sample_file), offset=5, limit=3)
        assert "5| line 5" in result
        assert "7| line 7" in result
        assert "8| line 8" not in result
        assert "Use offset=8 to continue" in result

    @pytest.mark.asyncio
    async def test_offset_beyond_end(self, tool, sample_file):
        result = await tool.execute(path=str(sample_file), offset=999)
        assert "Error" in result
        assert "beyond end" in result

    @pytest.mark.asyncio
    async def test_end_of_file_marker(self, tool, sample_file):
        result = await tool.execute(path=str(sample_file), offset=1, limit=9999)
        assert "End of file" in result

    @pytest.mark.asyncio
    async def test_empty_file(self, tool, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await tool.execute(path=str(f))
        assert "Empty file" in result

    @pytest.mark.asyncio
    async def test_image_file_returns_multimodal_blocks(self, tool, tmp_path):
        f = tmp_path / "pixel.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-data")

        result = await tool.execute(path=str(f))

        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert result[0]["_meta"]["path"] == str(f)
        assert result[1] == {"type": "text", "text": f"(Image file: {f})"}

    @pytest.mark.asyncio
    async def test_file_not_found(self, tool, tmp_path):
        result = await tool.execute(path=str(tmp_path / "nope.txt"))
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_missing_path_returns_clear_error(self, tool):
        result = await tool.execute()
        assert result == "Error reading file: Unknown path"

    @pytest.mark.asyncio
    async def test_char_budget_trims(self, tool, tmp_path):
        """When the selected slice exceeds _MAX_CHARS the output is trimmed."""
        f = tmp_path / "big.txt"
        # Each line is ~110 chars, 2000 lines ≈ 220 KB > 128 KB limit
        f.write_text("\n".join("x" * 110 for _ in range(2000)), encoding="utf-8")
        result = await tool.execute(path=str(f))
        assert len(result) <= ReadFileTool._MAX_CHARS + 500  # small margin for footer
        assert "Use offset=" in result


# ---------------------------------------------------------------------------
# _find_match  (unit tests for the helper)
# ---------------------------------------------------------------------------

class TestFindMatch:

    def test_exact_match(self):
        match, count = _find_match("hello world", "world")
        assert match == "world"
        assert count == 1

    def test_exact_no_match(self):
        match, count = _find_match("hello world", "xyz")
        assert match is None
        assert count == 0

    def test_crlf_normalisation(self):
        # Caller normalises CRLF before calling _find_match, so test with
        # pre-normalised content to verify exact match still works.
        content = "line1\nline2\nline3"
        old_text = "line1\nline2\nline3"
        match, count = _find_match(content, old_text)
        assert match is not None
        assert count == 1

    def test_line_trim_fallback(self):
        content = "    def foo():\n        pass\n"
        old_text = "def foo():\n    pass"
        match, count = _find_match(content, old_text)
        assert match is not None
        assert count == 1
        # The returned match should be the *original* indented text
        assert "    def foo():" in match

    def test_line_trim_multiple_candidates(self):
        content = "  a\n  b\n  a\n  b\n"
        old_text = "a\nb"
        match, count = _find_match(content, old_text)
        assert count == 2

    def test_empty_old_text(self):
        match, count = _find_match("hello", "")
        # Empty string is always "in" any string via exact match
        assert match == ""


# ---------------------------------------------------------------------------
# EditFileTool
# ---------------------------------------------------------------------------

class TestEditFileTool:

    @pytest.fixture()
    def tool(self, tmp_path):
        return EditFileTool(workspace=tmp_path)

    @pytest.mark.asyncio
    async def test_exact_match(self, tool, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello world", encoding="utf-8")
        result = await tool.execute(path=str(f), old_text="world", new_text="earth")
        assert "Successfully" in result
        assert f.read_text() == "hello earth"

    @pytest.mark.asyncio
    async def test_crlf_normalisation(self, tool, tmp_path):
        f = tmp_path / "crlf.py"
        f.write_bytes(b"line1\r\nline2\r\nline3")
        result = await tool.execute(
            path=str(f), old_text="line1\nline2", new_text="LINE1\nLINE2",
        )
        assert "Successfully" in result
        raw = f.read_bytes()
        assert b"LINE1" in raw
        # CRLF line endings should be preserved throughout the file
        assert b"\r\n" in raw

    @pytest.mark.asyncio
    async def test_trim_fallback(self, tool, tmp_path):
        f = tmp_path / "indent.py"
        f.write_text("    def foo():\n        pass\n", encoding="utf-8")
        result = await tool.execute(
            path=str(f), old_text="def foo():\n    pass", new_text="def bar():\n    return 1",
        )
        assert "Successfully" in result
        assert "bar" in f.read_text()

    @pytest.mark.asyncio
    async def test_ambiguous_match(self, tool, tmp_path):
        f = tmp_path / "dup.py"
        f.write_text("aaa\nbbb\naaa\nbbb\n", encoding="utf-8")
        result = await tool.execute(path=str(f), old_text="aaa\nbbb", new_text="xxx")
        assert "appears" in result.lower() or "Warning" in result

    @pytest.mark.asyncio
    async def test_replace_all(self, tool, tmp_path):
        f = tmp_path / "multi.py"
        f.write_text("foo bar foo bar foo", encoding="utf-8")
        result = await tool.execute(
            path=str(f), old_text="foo", new_text="baz", replace_all=True,
        )
        assert "Successfully" in result
        assert f.read_text() == "baz bar baz bar baz"

    @pytest.mark.asyncio
    async def test_not_found(self, tool, tmp_path):
        f = tmp_path / "nf.py"
        f.write_text("hello", encoding="utf-8")
        result = await tool.execute(path=str(f), old_text="xyz", new_text="abc")
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_missing_new_text_returns_clear_error(self, tool, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello", encoding="utf-8")
        result = await tool.execute(path=str(f), old_text="hello")
        assert result == "Error editing file: Unknown new_text"


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------

class TestListDirTool:

    @pytest.fixture()
    def tool(self, tmp_path):
        return ListDirTool(workspace=tmp_path)

    @pytest.fixture()
    def populated_dir(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "src" / "utils.py").write_text("pass")
        (tmp_path / "README.md").write_text("hi")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").mkdir()
        return tmp_path

    @pytest.mark.asyncio
    async def test_basic_list(self, tool, populated_dir):
        result = await tool.execute(path=str(populated_dir))
        assert "README.md" in result
        assert "src" in result
        # .git and node_modules should be ignored
        assert ".git" not in result
        assert "node_modules" not in result

    @pytest.mark.asyncio
    async def test_recursive(self, tool, populated_dir):
        result = await tool.execute(path=str(populated_dir), recursive=True)
        # Normalize path separators for cross-platform compatibility
        normalized = result.replace("\\", "/")
        assert "src/main.py" in normalized
        assert "src/utils.py" in normalized
        assert "README.md" in result
        # Ignored dirs should not appear
        assert ".git" not in result
        assert "node_modules" not in result

    @pytest.mark.asyncio
    async def test_max_entries_truncation(self, tool, tmp_path):
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text("x")
        result = await tool.execute(path=str(tmp_path), max_entries=3)
        assert "truncated" in result
        assert "3 of 10" in result

    @pytest.mark.asyncio
    async def test_empty_dir(self, tool, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        result = await tool.execute(path=str(d))
        assert "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_not_found(self, tool, tmp_path):
        result = await tool.execute(path=str(tmp_path / "nope"))
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_missing_path_returns_clear_error(self, tool):
        result = await tool.execute()
        assert result == "Error listing directory: Unknown path"


# ---------------------------------------------------------------------------
# Workspace restriction + extra_allowed_dirs
# ---------------------------------------------------------------------------

class TestWorkspaceRestriction:

    @pytest.mark.asyncio
    async def test_read_blocked_outside_workspace(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("top secret")

        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=str(secret))
        assert "Error" in result
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_read_allowed_with_extra_dir(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "test_skill" / "SKILL.md"
        skill_file.parent.mkdir()
        skill_file.write_text("# Test Skill\nDo something.")

        tool = ReadFileTool(
            workspace=workspace, allowed_dir=workspace,
            extra_allowed_dirs=[skills_dir],
        )
        result = await tool.execute(path=str(skill_file))
        assert "Test Skill" in result
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_read_allowed_in_media_dir(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        media_file = media_dir / "photo.txt"
        media_file.write_text("shared media", encoding="utf-8")

        monkeypatch.setattr("nanobot.agent.tools.path_utils.get_media_dir", lambda: media_dir)

        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=str(media_file))
        assert "shared media" in result
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_extra_dirs_does_not_widen_write(self, tmp_path):
        from nanobot.agent.tools.filesystem import WriteFileTool

        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=str(outside / "hack.txt"), content="pwned")
        assert "Error" in result
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_read_still_blocked_for_unrelated_dir(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        unrelated = tmp_path / "other"
        unrelated.mkdir()
        secret = unrelated / "secret.txt"
        secret.write_text("nope")

        tool = ReadFileTool(
            workspace=workspace, allowed_dir=workspace,
            extra_allowed_dirs=[skills_dir],
        )
        result = await tool.execute(path=str(secret))
        assert "Error" in result
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_workspace_file_still_readable_with_extra_dirs(self, tmp_path):
        """Adding extra_allowed_dirs must not break normal workspace reads."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        ws_file = workspace / "README.md"
        ws_file.write_text("hello from workspace")
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        tool = ReadFileTool(
            workspace=workspace, allowed_dir=workspace,
            extra_allowed_dirs=[skills_dir],
        )
        result = await tool.execute(path=str(ws_file))
        assert "hello from workspace" in result
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_edit_blocked_in_extra_dir(self, tmp_path):
        """edit_file must not be able to modify files in extra_allowed_dirs."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "weather" / "SKILL.md"
        skill_file.parent.mkdir()
        skill_file.write_text("# Weather\nOriginal content.")

        tool = EditFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(
            path=str(skill_file),
            old_text="Original content.",
            new_text="Hacked content.",
        )
        assert "Error" in result
        assert "outside" in result.lower()
        assert skill_file.read_text() == "# Weather\nOriginal content."


# ---------------------------------------------------------------------------
# CopyFileTool
# ---------------------------------------------------------------------------

class TestCopyFileTool:

    @pytest.fixture()
    def tool(self, tmp_path):
        return CopyFileTool(workspace=tmp_path, allowed_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_copy_file_basic(self, tool, tmp_path):
        """Copy file from temp to workspace, verify content matches."""
        import json

        source = tmp_path / "source.txt"
        source.write_text("Hello, World!", encoding="utf-8")
        dest = tmp_path / "dest.txt"

        result = await tool.execute(source=str(source), dest=str(dest))
        data = json.loads(result)

        assert data["path"] == str(dest)
        assert data["size"] == 13
        assert dest.read_text() == "Hello, World!"

    @pytest.mark.asyncio
    async def test_copy_file_dest_is_directory(self, tool, tmp_path):
        """When dest is dir, use source filename."""
        import json

        source = tmp_path / "document.md"
        source.write_text("# My Doc", encoding="utf-8")
        dest_dir = tmp_path / "docs"
        dest_dir.mkdir()

        result = await tool.execute(source=str(source), dest=str(dest_dir))
        data = json.loads(result)

        assert data["path"] == str(dest_dir / "document.md")
        assert (dest_dir / "document.md").read_text() == "# My Doc"

    @pytest.mark.asyncio
    async def test_copy_file_overwrite_false_fails(self, tool, tmp_path):
        """dest exists, overwrite=false -> error."""
        source = tmp_path / "src.txt"
        source.write_text("new content", encoding="utf-8")
        dest = tmp_path / "existing.txt"
        dest.write_text("old content", encoding="utf-8")

        result = await tool.execute(source=str(source), dest=str(dest), overwrite=False)

        assert "Error" in result
        assert "already exists" in result
        assert dest.read_text() == "old content"

    @pytest.mark.asyncio
    async def test_copy_file_overwrite_true_succeeds(self, tool, tmp_path):
        """dest exists, overwrite=true -> success."""
        import json

        source = tmp_path / "src.txt"
        source.write_text("new content", encoding="utf-8")
        dest = tmp_path / "existing.txt"
        dest.write_text("old content", encoding="utf-8")

        result = await tool.execute(source=str(source), dest=str(dest), overwrite=True)
        data = json.loads(result)

        assert data["path"] == str(dest)
        assert dest.read_text() == "new content"

    @pytest.mark.asyncio
    async def test_copy_file_create_parents(self, tool, tmp_path):
        """Parent dirs don't exist, create_parents=true -> creates them."""
        import json

        source = tmp_path / "source.txt"
        source.write_text("content", encoding="utf-8")
        dest = tmp_path / "a" / "b" / "c" / "dest.txt"

        result = await tool.execute(source=str(source), dest=str(dest), create_parents=True)
        data = json.loads(result)

        assert data["path"] == str(dest)
        assert dest.exists()
        assert dest.read_text() == "content"

    @pytest.mark.asyncio
    async def test_copy_file_no_create_parents_fails(self, tool, tmp_path):
        """Parent dirs missing, create_parents=false -> error."""
        source = tmp_path / "source.txt"
        source.write_text("content", encoding="utf-8")
        dest = tmp_path / "nonexistent" / "dest.txt"

        result = await tool.execute(source=str(source), dest=str(dest), create_parents=False)

        assert "Error" in result
        assert "Parent directory does not exist" in result
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_copy_file_source_outside_allowlist(self, tmp_path, monkeypatch):
        """Source not in allowlist -> error."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        source = outside / "secret.txt"
        source.write_text("secret data", encoding="utf-8")

        # Mock get_media_dir to return a dir that doesn't contain our source
        monkeypatch.setattr("nanobot.agent.tools.path_utils.get_media_dir", lambda: workspace / "media")

        tool = CopyFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(source=str(source), dest=str(workspace / "copy.txt"))

        assert "Error" in result
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_copy_file_returns_path_and_size(self, tool, tmp_path):
        """Verify return value format."""
        import json

        source = tmp_path / "data.bin"
        source.write_bytes(b"\x00" * 1024)

        result = await tool.execute(source=str(source), dest=str(tmp_path / "copy.bin"))
        data = json.loads(result)

        assert "path" in data
        assert "size" in data
        assert data["size"] == 1024
        assert data["path"].endswith("copy.bin")

    @pytest.mark.asyncio
    async def test_copy_file_source_from_media_dir(self, tmp_path, monkeypatch):
        """Source in media dir should be allowed."""
        import json

        workspace = tmp_path / "ws"
        workspace.mkdir()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        source = media_dir / "uploaded.pdf"
        source.write_bytes(b"%PDF-fake")

        monkeypatch.setattr("nanobot.agent.tools.path_utils.get_media_dir", lambda: media_dir)

        tool = CopyFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(source=str(source), dest=str(workspace / "doc.pdf"))
        data = json.loads(result)

        assert data["path"] == str(workspace / "doc.pdf")
        assert (workspace / "doc.pdf").read_bytes() == b"%PDF-fake"

    @pytest.mark.asyncio
    async def test_copy_file_dest_outside_workspace_fails(self, tmp_path, monkeypatch):
        """Dest outside workspace -> error."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        source = workspace / "file.txt"
        source.write_text("data", encoding="utf-8")

        monkeypatch.setattr("nanobot.agent.tools.path_utils.get_media_dir", lambda: workspace / "media")

        tool = CopyFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(source=str(source), dest=str(outside / "stolen.txt"))

        assert "Error" in result
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_copy_file_source_not_found(self, tool, tmp_path):
        """Source file doesn't exist -> error."""
        result = await tool.execute(
            source=str(tmp_path / "nonexistent.txt"),
            dest=str(tmp_path / "dest.txt"),
        )

        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_copy_file_source_is_directory(self, tool, tmp_path):
        """Source is a directory -> error."""
        source_dir = tmp_path / "src_dir"
        source_dir.mkdir()

        result = await tool.execute(
            source=str(source_dir),
            dest=str(tmp_path / "dest.txt"),
        )

        assert "Error" in result
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_copy_file_missing_source(self, tool):
        """Missing source param -> error."""
        result = await tool.execute(dest="/some/path")
        assert "Error" in result
        assert "source" in result.lower()

    @pytest.mark.asyncio
    async def test_copy_file_missing_dest(self, tool, tmp_path):
        """Missing dest param -> error."""
        source = tmp_path / "file.txt"
        source.write_text("x", encoding="utf-8")
        result = await tool.execute(source=str(source))
        assert "Error" in result
        assert "dest" in result.lower()
