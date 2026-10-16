---
name: noname-init
description: 在当前项目启用 NoName 记忆账本。用于初始化 NoName、开启项目记忆或让当前 Codex 项目开始记录对话。
---

Run `"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" init` in the current workspace.
Then run the same executable with `status` and show the resolved workspace and database path.
The default is the Git checkout root, or CWD outside Git. If the user specifies a narrower
root, pass `--root <absolute-directory>` before the subcommand.

Explain that lifecycle capture requires trusted hooks and begins on subsequent hook events.
Initialization does not import old chats. It enables only this project's local ledger.
Codex owns tool/model execution; NoName's AgentLoop and Sandbox do not intercept it.
