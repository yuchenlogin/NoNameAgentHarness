---
name: noname-remember
description: 将指定内容或当前工作进度保存到 NoName，生成可审核的记忆候选。用于记住决定、保存进度或创建接续检查点，不自动批准长期记忆。
---

Use `"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" remember --key <key> --text-file <file>`.
Write the exact note to a temporary UTF-8 file with a file tool; never interpolate note
content into shell code. Before choosing a key, inspect `recall` and `inbox` for the same
topic. Reuse its existing layer and logical_key for a correction or progress update;
do not create a new key just because wording changed. Use a user-supplied key when given,
or `checkpoint` for the current work checkpoint. Keep unrelated decisions on distinct keys.
The default layer is mid (current task). Use `--layer high` only for an explicitly requested
stable project fact or decision. Use CODEX_THREAD_ID or the startup session ID; pass
`--session <id>` if needed. Without an ID, use a clearly labeled `manual-<unique-id>` session
and disclose that it is a manual checkpoint, not a captured Codex session.

Keep notes selective: decisions and their reasons, verified state and a next action or source
pointer. Raw conversation and routine tool output belong to the archive, not durable state.
If there is no new decision, correction or meaningful phase boundary, do not manufacture a
checkpoint. When asked to save progress without supplied text, summarize completed work,
verification, open questions and next steps. Label this as an assistant summary, not a verbatim
user quote. Add `--reason <reason>` and `--next-step <next action>` when available; safely
quote these arguments just like paths. Do not infer taste or aesthetics as project facts;
present them for explicit human confirmation through the existing taste candidate workflow.

Read the result: `status: proposed` means a new pending candidate; `already_pending` reuses
the latest identical candidate; `unchanged` means the same content is already active and no
candidate was created (`proposal: null`). Report actual IDs and active status. Explain the
`change.before` → `change.after` preview for corrections; `supersedes_on_accept` is conditional
on approval. Related older pending IDs are not automatically rejected or approved.
Saving is a proposal; approval is a separate
explicit user review decision through `$noname-review`. Do not infer approval from “save”.
