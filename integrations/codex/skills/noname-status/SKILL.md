---
name: noname-status
description: 检查 NoName 是否已启用、是否在当前聊天实际运行，显示账本路径、记忆数量、hook 回执和完整性校验结果。
---

Run `"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" status` in the current workspace.
If the current session ID is supplied by NoName startup context, add `--session <id>`.
Otherwise the executable uses CODEX_THREAD_ID when available; do not invent a session ID.

Report enabled, database, event_count, accepted_state_count, pending_review and integrity.ok.
Distinguish global availability, project initialization and observed hook execution.
Use current_session_hooks_observed to identify observed lifecycle stages, then inspect
matching-session receipts: captured_text true proves body capture; false means no body
was available. context_prepared true means startup context was generated, not that the
model adopted it. Receipts from other sessions prove nothing about this session.
Installation alone does not prove hook trust or that memory was loaded.
When no current session is available, say that session-specific execution is unverified.
If disabled, explain `$noname-init`. If receipts are absent, explain the CLI `/hooks` trust
step and restarting/resuming a chat. Do not search unrelated project files to infer activation.
