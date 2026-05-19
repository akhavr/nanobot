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

## Upgrade Path: Per-User Workspace Directories

The current file-level isolation (`USER_{id}.md`, per-user memory) is suitable for most deployments. For **multi-tenant commercial deployments** requiring hard isolation, a future upgrade path is planned.

### Current: File-Level Isolation

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

**Trade-offs:**
- All user files share one directory (easy backup, single config)
- File-system ACLs cannot isolate individual users
- Suitable for trusted team deployments

### Future: Per-User Workspace Directories

```
workspace/
  users/
    334424084/
      USER.md
      USER_PRIVATE.md
      memory/
        MEMORY.md
        history.jsonl
    987654321/
      USER.md
      USER_PRIVATE.md
      memory/
        MEMORY.md
        history.jsonl
  SHARED.md                   # Still shared across all users
```

**Benefits:**
- Each user gets an isolated directory (`workspace/users/{user_id}/`)
- File-system permissions can enforce hard boundaries
- Per-user quota enforcement possible
- Easier per-user backup/export/deletion

### When to Upgrade

Consider upgrading to per-user workspace directories when:

| Scenario | File-Level | Per-User Workspace |
|----------|------------|-------------------|
| Single user or small trusted team | ✓ | overkill |
| Multi-tenant SaaS with billing | — | ✓ |
| Regulatory requirement for data isolation | — | ✓ |
| Need per-user filesystem quotas | — | ✓ |
| Simple ops, single backup path | ✓ | — |

The per-user workspace feature is not yet implemented. If you need it for a commercial deployment, please open an issue describing your use case.
