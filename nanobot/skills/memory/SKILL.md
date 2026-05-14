---
name: memory
description: Three-layer memory system with privacy separation and Dream-managed consolidation.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences (communication style, timezone). **Immediate saves allowed.** Loaded everywhere.
- `USER_PRIVATE.md` — Private info (solo trips, dating, health, finances). **DM/1:1 only.** Never loaded in multi-user groups.
- `SHARED.md` — Shareable facts (family, birthdays, dietary). **Immediate saves allowed.** Loaded everywhere.
- `memory/MEMORY.md` — Long-term facts (project context, important events). **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.

## Privacy Routing

When saving learned facts, route to the appropriate file:

| Topic | File | Loaded In |
|-------|------|-----------|
| Solo trips (without family) | USER_PRIVATE.md | DM/1:1 only |
| Dating/social interests | USER_PRIVATE.md | DM/1:1 only |
| Health research (screenings, treatments) | USER_PRIVATE.md | DM/1:1 only |
| Finances (budget, spending, investments) | USER_PRIVATE.md | DM/1:1 only |
| Communication style | USER.md | Everywhere |
| Response preferences | USER.md | Everywhere |
| Timezone, language | USER.md | Everywhere |
| Family members | SHARED.md | Everywhere |
| Birthdays, anniversaries | SHARED.md | Everywhere |
| Allergies, dietary restrictions | SHARED.md | Everywhere |
| Home location | SHARED.md | Everywhere |
| Group trip plans | SHARED.md | Everywhere |

### Private Info in Group Chats

If the user shares private information (solo trips, dating, health, finances) in a **multi-user group chat**:
1. Save to `USER_PRIVATE.md` (protect them from future exposure)
2. **Do NOT** mention or reference this info in the group response
3. If appropriate, briefly acknowledge in DM: "I noticed something private in [Group]. Saved to private memory."

## Immediate Saves (Critical Facts)

When the user shares **critical personal information**, save it immediately using `edit_file`. Do NOT wait for Dream.

### What to save immediately (by file)

**USER.md** (preferences, loaded everywhere):
- Identity: Name, timezone, location, language
- Communication style preferences

**SHARED.md** (shareable facts, loaded everywhere):
- Family members: Names, relationships, birthdays
- Allergies, dietary restrictions
- Critical dates: Birthdays, anniversaries

**USER_PRIVATE.md** (private, DM/1:1 only):
- Solo travel plans
- Dating/personal interests
- Health research (not shareable conditions)
- Financial details

### How to save

1. Determine the correct file based on privacy routing (see table above)
2. Use `edit_file` to update the appropriate section
3. For family members, add a row to `SHARED.md`: `| Name | Relation | Notes |`
4. After saving, briefly acknowledge: "I've noted that [fact]." (one line)

### Example: Shareable fact (in any chat)

User says: "My daughter Emma turns 15 on October 1st"

Action: `edit_file` on `SHARED.md` to add to Family Members table:
```
| Emma | daughter | Birthday: October 1 |
```

### Example: Private fact (protect if in group)

User says in a group: "I'm planning a solo trip to Cancun, maybe meet some people there"

Action:
1. `edit_file` on `USER_PRIVATE.md` to add to Solo Travel section
2. Do NOT mention dating context in group response
3. Consider DM: "I saved your solo trip plans to private memory."

## Bulk Extraction (Dream)

Less critical information is extracted by Dream during scheduled consolidation:

- Communication preferences and style
- Food/drink preferences
- Work context (role, company, projects)
- Activity preferences
- Patterns observed over time

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with `cursor`, `timestamp`, `content`.

- For broad searches, start with `grep(..., path="memory", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.jsonl", output_mode="content", case_insensitive=true)`

## Important

- **Do NOT edit SOUL.md or MEMORY.md.** They are automatically managed by Dream.
- `USER.md`, `USER_PRIVATE.md`, and `SHARED.md` allow immediate edits for critical facts.
- `USER_PRIVATE.md` is **never loaded** in chats with 3+ members (privacy protection).
- If you notice outdated information in Dream-managed files, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
