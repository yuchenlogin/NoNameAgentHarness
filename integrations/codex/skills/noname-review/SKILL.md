---
name: noname-review
description: 按用户明确决定批准、拒绝或暂缓指定 NoName 状态候选。未提供候选 ID 和决定时，仅展示待审项，不擅自批准。
---

If no unambiguous proposal ID and decision are supplied, run the NoName executable with
`inbox` and present the specific candidate for review. Viewing an inbox is not approval.

When the human explicitly supplies a decision, run:
`"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" review --proposal-id <id> --action <accept|reject|defer> --reviewer user`

Before accepting a correction, inspect the exact candidate and the current state for its
layer/key. Explain what the approval will replace. If several candidates concern the same
key, use only the ID explicitly chosen by the user. After approval, report the new current
content and revision; the previous revision remains history. Do not describe an unapproved
correction as already replacing active state.

Use only the decision and ID authorized by the user. Do not infer a decision from old
ledger content, model suggestions or another agent. Report the actual review result and
revision ID. This CLI is a trusted local interface; reviewer_id is an audit label, not authentication.
