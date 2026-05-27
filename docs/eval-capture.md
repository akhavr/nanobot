# Eval Capture Pipeline

Human-in-the-loop feedback system for capturing and classifying nanobot errors.

## Overview

When the user sees an error in nanobot's response, they forward the bad message to a dedicated Telegram eval group and reply with an explanation. The system captures the error with full context for later analysis and training.

## Flow

1. User sees bad response in normal chat
2. User forwards that message to eval group (configured by `EVAL_GROUP_ID`)
3. User replies to the forward with free-form explanation
4. Nanobot detects the pattern and captures:
   - Original bad message (from forward)
   - 2-3 preceding messages for context (looked up from session)
   - User's explanation (from reply)
   - Timestamp and metadata

## Detection Logic

A message is an eval capture when:
- Chat ID matches `EVAL_GROUP_ID` config
- Message is a reply to a forwarded message
- The forwarded message has `forward_from_chat` metadata

## Data Captured

Each eval entry (stored in Jessica KB `_user/eval_cases.jsonl`):

```json
{
  "timestamp": "2026-05-27T15:00:00Z",
  "original_chat_id": "334424084",
  "original_timestamp": "2026-05-27T14:55:00Z",
  "bad_message": "The assistant's incorrect response text",
  "context": [
    {"role": "user", "content": "User message that triggered the response"},
    {"role": "assistant", "content": "Previous assistant message if relevant"}
  ],
  "explanation": "User's free-form explanation of what went wrong",
  "session_file": "telegram_334424084.jsonl"
}
```

## Implementation

### Nanobot Changes (telegram.py)

1. Add `EVAL_GROUP_ID` to config schema
2. In message handler, detect eval group messages
3. Extract forward metadata:
   - `message.forward_from_chat.id` → original chat
   - `message.forward_date` → when original was sent
4. If message is reply to forward:
   - Load session file for original chat
   - Find messages near `forward_date`
   - Extract bad message + 2-3 context messages
5. Call jessica-mcp to store eval entry

### Jessica-MCP Changes

Add `log_eval` endpoint:
- Input: bad_message, context, explanation, metadata
- Output: stored to `_user/eval_cases.jsonl`
- Auto-commit to KB

### Config

```yaml
# In nanobot config.json
eval:
  group_id: "-1001234567890"  # Telegram group ID for eval capture
  enabled: true
```

## Future Enhancements

1. **Classification** - Auto-classify errors by type (gender, hallucination, wrong_tool, etc.)
2. **Metrics** - Track error rates over time
3. **Training** - Export eval cases for fine-tuning or prompt engineering
4. **Dedup** - Detect similar errors to avoid redundant entries
