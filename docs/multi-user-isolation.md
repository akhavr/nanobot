# Multi-User Workspace Isolation

## Overview

Nanobot supports per-user isolation of private data to prevent data leakage between users sharing the same bot instance.

## Activation

Enable in `config.json`:

```json
{
  "multiUser": true,
  ...
}
```

Default is `false` (single-user mode, backward compatible).

## File Structure

### Single-user mode (default)

```
workspace/
  USER.md              # User profile
  USER_PRIVATE.md      # Private data (DMs only)
  SHARED.md            # Shared data (all contexts)
  memory/
    MEMORY.md          # Long-term memory
    history.jsonl      # Dream extractions
```

### Multi-user mode

```
workspace/
  USER_334424084.md           # User 334424084's profile
  USER_PRIVATE_334424084.md   # User 334424084's private data
  USER_987654321.md           # User 987654321's profile
  USER_PRIVATE_987654321.md   # User 987654321's private data
  SHARED.md                   # Shared data (unchanged)
  memory/
    MEMORY_334424084.md       # User 334424084's long-term memory
    history_334424084.jsonl   # User 334424084's Dream extractions
    MEMORY_987654321.md       # User 987654321's long-term memory
    history_987654321.jsonl   # User 987654321's Dream extractions
```

## Identifier

Files use the **channel user ID** (e.g., Telegram user ID):
- Immutable (unlike usernames)
- Available in every message metadata
- Works across channels

## Context Loading Rules

| File Type | Single-user | Multi-user |
|-----------|-------------|------------|
| SOUL.md | Loaded always | Loaded always |
| AGENTS.md | Loaded always | Loaded always |
| TOOLS.md | Loaded always | Loaded always |
| SHARED.md | Loaded always | Loaded always |
| USER.md | Loaded always | `USER_{user_id}.md` |
| USER_PRIVATE.md | Loaded when member_count ≤ 2 | `USER_PRIVATE_{user_id}.md` when member_count ≤ 2 |

## Dream Consolidation

### Private sessions (DMs, 1:1 with bot)
- Single-user: writes to `USER.md`, `USER_PRIVATE.md`
- Multi-user: writes to `USER_{user_id}.md`, `USER_PRIVATE_{user_id}.md`

### Group sessions
- Both modes: writes to `SHARED.md` only
- Facts shared in groups become shared facts regardless of who said them

## New Users

When a new user sends their first message in multi-user mode:
- No files exist yet — bot loads only SHARED.md context
- Dream creates user files as it extracts facts from conversations
- Context builds organically over time

## Migration

To migrate from single-user to multi-user:

1. Enable the flag:
   ```json
   { "multiUser": true }
   ```

2. Rename existing files (replace `YOUR_USER_ID` with your Telegram user ID):
   ```bash
   cd ~/.nanobot/workspace
   mv USER.md USER_YOUR_USER_ID.md
   mv USER_PRIVATE.md USER_PRIVATE_YOUR_USER_ID.md
   mv memory/MEMORY.md memory/MEMORY_YOUR_USER_ID.md
   mv memory/history.jsonl memory/history_YOUR_USER_ID.jsonl
   ```

3. Restart nanobot

## Privacy Model

| Context | What's loaded |
|---------|--------------|
| DM with user A | SHARED.md + USER_A.md + USER_PRIVATE_A.md + MEMORY_A.md |
| DM with user B | SHARED.md + USER_B.md + USER_PRIVATE_B.md + MEMORY_B.md |
| Group chat | SHARED.md only (no user-specific files) |

User A's private data never leaks to User B's context, and vice versa.
