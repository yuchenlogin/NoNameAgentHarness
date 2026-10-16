---
name: noname-inbox
description: 查看 NoName 待审核记忆、任务状态、偏好与卡片候选，显示候选 ID、内容、来源和冲突，便于逐条决定是否采纳。
---

Run `"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" inbox`.
Show pending IDs, summaries, source events and conflicts. Distinguish candidates from active
memory. Do not issue review decisions when the user only asks to see the inbox.
Lead with a one-sentence description of each meaningful change and why it matters. For a
correction, show the current content next to the proposed replacement (inspect `recall` or
the ledger for the same layer/key), not just conflict IDs. Group pending candidates for the
same layer/key together, identify older alternatives, and let the user choose; do not
automatically reject them or present every old alternative as a separate new task.
For state proposals, explain `$noname-review <proposal-id> accept` or `reject`.
Taste and card candidates use the core CLI taste-review/card-review commands; this bridge's
review command only handles state proposals. Do not pretend all candidate IDs are interchangeable.
