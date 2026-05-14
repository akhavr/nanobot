---
name: memory
description: Two-layer memory system with immediate saves for critical facts and Dream-managed consolidation.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Immediate saves allowed** for critical facts. Dream also consolidates here.
- `memory/MEMORY.md` — Long-term facts (project context, important events). **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.

## Immediate Saves (Critical Facts)

When the user shares **critical personal information**, save it immediately to `USER.md` using `edit_file`. Do NOT wait for Dream.

### What to save immediately

- **Identity**: Name, timezone, location, language
- **Family members**: Names, relationships, birthdays (add rows to the Family table)
- **Health**: Allergies, medical conditions
- **Critical dates**: Birthdays, anniversaries

### How to save

1. Use `edit_file` to update the appropriate section in `USER.md`
2. For family members, add a row to the table: `| Name | Relation | Birthday | Notes |`
3. After saving, briefly acknowledge: "I've noted that [fact]." (one line, not blocking)

### Example

User says: "My daughter Emma turns 15 on October 1st"

Action: `edit_file` on `USER.md` to add table row:
```
| Emma | daughter | October 1 | turns 15 in [year] |
```

Response: "I've noted that Emma's birthday is October 1st."

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
- USER.md allows immediate edits for critical facts only (identity, family, health).
- If you notice outdated information in Dream-managed files, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
