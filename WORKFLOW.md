---
template_version: 4
agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  signal_method: auto  # "auto", "mcp", "text", "file"
  extra_args: []

tracker:
  kind: git-bug-graphql

workspace:
  kind: worktree
  base_branch: main
  root: .worktrees

notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID
    level: questions

merge:
  require_review: true
  review_label: reviewed
  auto_merge_label: auto-merge

auto_start:
  enabled: true
  label: nightshift
  poll_interval_s: 30
  max_concurrent: 2

hooks:
  after_create: |
    echo "Workspace created"
  before_run: |
    echo "Starting agent run"
  after_run: |
    echo "Agent run finished"
  timeout_s: 60

terminal_statuses:
  - closed
---

You are working on the following issue:

**Title:** {{ issue.title }}
**Description:**
{{ issue.body }}

{% if attempt %}
This is continuation attempt {{ attempt }}. Review previous work and continue.
{% endif %}

**Related previous issues:**
{{ related_context }}

RULES:
1. Work on the current branch. The repo is already checked out.
2. Before starting work, check if your branch is behind the base branch:
   `git log --oneline HEAD..{{ base_branch }} | head -5`
   If commits are shown, rebase first: `git fetch origin && git rebase origin/{{ base_branch }}`
   Resolve any conflicts before proceeding.
3. If you have a blocking question, include all relevant context IN the question
   itself (code snippets, file paths, what you did, options you see) — the human
   reads ONLY the question text, they cannot see your other output.
4. Commit frequently. Write tests where appropriate.
5. Before signaling completion, always commit your changes:
   `git add -A && git commit -m "descriptive message"`
   Never signal completion with uncommitted work.

For bug fixes, follow this protocol:
1. Reproduce the bug — run the failing scenario and confirm the symptom.
2. Minimize — isolate the smallest code surface that triggers it.
3. Write a failing test that captures the exact bug.
4. Fix the code and verify the test passes.
5. Confirm the original reproduction scenario no longer fails.
6. Search for similar patterns elsewhere in the codebase and fix them too.

{% if agent_kind == "openhands" %}
## Signal Protocol
When you complete the task, write a file at /session/signal/done containing a one-line summary.
If you have a question, write /session/signal/question.json with {"question": "your question"}.
For progress checkpoints, write /session/signal/checkpoint with a description.
These signal files are REQUIRED in addition to using FinishAction.
{% endif %}
{% if agent_kind == "codex" %}
## Signal Protocol
Use MCP tools from the nightshift-signals server to signal lifecycle events:
- Call `nightshift_done` with a summary when the task is complete.
- Call `nightshift_checkpoint` with a description for progress updates.
- Call `nightshift_question` with your question if you need human input.
These MCP tools are REQUIRED. Do NOT print text markers directly.
{% endif %}

## Feedback Logging

When you receive a `@nightshift revise` with reviewer feedback, verify each claim:
1. Read the file and line mentioned
2. Determine if the reviewer's claim is accurate

Append your assessment to `.nightshift/reviewer-issues.yaml`:
```yaml
- category: <false_positive|partial|valid>
  session: {{ session_id }}
  date: {{ date }}
  file: <path>
  claimed: <what reviewer said>
  actual: <what the code actually does>
  verdict: <agree|false_positive|partial>
  reason: <one-line explanation>
```
This logs patterns in reviewer feedback quality for later analysis.

Begin by reading the codebase, then plan your approach.
