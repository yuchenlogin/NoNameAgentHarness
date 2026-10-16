---
name: noname-search
description: 用关键词搜索当前项目的 NoName 账本、已记录对话和工作笔记，支持中文检索，返回事件 ID、所属聊天和相关证据。
---

Run `"${CODEX_HOME:-$HOME/.codex}/bin/noname-codex" search <query>` with the user's query
as a safely quoted argument. Show relevant event IDs, session IDs and evidence text.
Search only covers this project's recorded ledger; no matches do not establish that
the topic never occurred in older uncaptured chats. Search results are evidence, not approved facts.
