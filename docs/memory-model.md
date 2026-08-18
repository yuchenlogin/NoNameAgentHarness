# 可溯源记忆模型

> 本文定义 NoName 的长期记忆语义。目标不是让模型「记得更多」，而是让每条长期记忆都能回答：从哪里来、谁提议、谁批准、何时有效、被什么取代。

## 1. 第一原则：保留证据，投影记忆

NoName 不把上下文摘要当作事实来源。完整性通过两层获得：

- **证据层无损**：会话事件、工具结果、文件 diff 与模型响应只增不改；
- **记忆层可用**：模型从证据中抽取可检索的候选记忆，但每条都保留 source span。

所谓「关键记忆提取」不是删除不重要内容，而是在完整证据之上建立一个可版本化索引。遗漏可以在未来重新提取，错误可以失效，原始证据始终存在。

## 2. 记忆类型与作用域

| 类型 | 例子 | 默认作用域 | 是否需审核 |
|---|---|---|---|
| Fact | 项目使用 SQLite；接口返回 JSON | 项目 | 冲突或跨会话时需要 |
| Decision / Canon | 为什么选择事件溯源 | 项目 | 必须 |
| Task State | 当前阻塞、下一步 | 会话/项目 | 低风险可自动，结束时汇总 |
| Episode | 一次失败与解决方式 | 项目/用户 | 进入长期层时需要 |
| Authored Taste | 用户主动声明的态度 | 用户/可选项目 | 用户写入即确认 |
| Adopted Taste | 用户采纳的模型涌现 | 用户/可选项目 | 必须显式采纳 |
| Procedure | 稳定工作流或提示策略 | agent/项目 | 启用前必须 |

作用域必须显式：`session / project / user / agent / organization`。跨项目传播只允许 `user` 或更高作用域的已审核条目。

## 3. 最小数据语义

下面是概念 schema，不是最终迁移文件：

```sql
CREATE TABLE session_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  UNIQUE(session_id, seq)
);

CREATE TABLE evidence_spans (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES session_events(id),
  artifact_uri TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  content_hash TEXT NOT NULL
);

CREATE TABLE memory_records (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  content_json TEXT NOT NULL,
  status TEXT NOT NULL,
  valid_from INTEGER,
  valid_to INTEGER,
  recorded_at INTEGER NOT NULL,
  supersedes_id TEXT REFERENCES memory_records(id),
  origin TEXT NOT NULL,
  actor_id TEXT NOT NULL
);

CREATE TABLE memory_evidence (
  memory_id TEXT NOT NULL REFERENCES memory_records(id),
  evidence_span_id TEXT NOT NULL REFERENCES evidence_spans(id),
  relation TEXT NOT NULL,
  PRIMARY KEY(memory_id, evidence_span_id, relation)
);

CREATE TABLE memory_reviews (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memory_records(id),
  action TEXT NOT NULL,
  reviewer_id TEXT NOT NULL,
  reason TEXT,
  reviewed_at INTEGER NOT NULL
);
```

关键语义：

- `session_events` 和 `evidence_spans` append-only；
- `memory_records` 不硬删，纠错写新版本并指向 `supersedes_id`；
- `valid_from/valid_to` 表示现实世界中何时有效，`recorded_at` 表示系统何时知道；
- 一个记忆可引用多个证据，一个证据可支持或反驳多个记忆；
- embedding、FTS5 与图只是索引，可重建，不是事实来源。

## 4. 提取管线

### 4.1 两阶段提取

1. **覆盖提取**：按事件类型扫描，尽量不遗漏候选，允许高召回；
2. **长期门控**：判断候选是否值得进入项目/用户长期层，追求高精度。

同一个模型不应同时负责「尽量多找」和「决定永久写入」。默认使用独立 reviewer 或用户审核，避免抽取器自我确认。

### 4.2 候选操作

借用 `ADD / UPDATE / NOOP` 的心智模型，但不用硬删除：

- `ADD`：新建候选；
- `SUPERSEDE`：用新版本替代旧版本，旧版本标记失效；
- `INVALIDATE`：声明旧记忆不再有效，保留历史；
- `NOOP`：无值得写入的变化；
- `SPLIT / MERGE`：审核时拆分或合并候选，保留来源关系。

### 4.3 完整性检查

一次上下文结束时，提取器至少按以下类别扫描：

- 用户明确要求记住的内容；
- 项目决策与理由；
- 未完成任务与阻塞；
- 产生或修改的文件；
- 工具失败、修复和可复用经验；
- 用户明确认可或否定的回答片段；
- 可能成为 Authored/Adopted Taste 的候选；
- 与现有法典或品味冲突的内容。

检查结果同样进入账本。没有候选也是一个有理由的结果。

## 5. 用户审核契约

审核 UI 对每条候选显示：

- 一句话提案；
- 影响范围和预计持续时间；
- 代表性原文和「查看完整上下文」；
- 与现有记忆的冲突或重复；
- 接受后的行为影响；
- 接受、编辑、拆分、拒绝、稍后处理。

法典写入应被设计成一种简短但郑重的签署行为。系统必须解释：花几十秒审核，是为了避免错误在未来数月里被反复放大。

## 6. 检索与上下文投影

推荐三阶段检索：

1. **召回**：FTS5 + 向量相似度 + 时间/作用域过滤；
2. **重排**：任务相关性、来源质量、审核状态、新鲜度、冲突；
3. **构造**：输出稳定的 prompt 片段，每条带短引用 id，必要时可下钻原文。

品味检索与事实检索分开运行，最后在上下文组装层合并。这样才能保证态度不会被误当成事实。

## 7. 技术借鉴边界

- SQLite 是事实基座，不是完整记忆产品；
- Graphiti/Zep 的双时序、失效和 episode 血缘值得借鉴，不必一开始引入图数据库；
- LangMem 的 profile/collection/episodic/procedural 分层值得借鉴，但其存储和审核必须自建；
- mem0 的管理器流程和 OpenMemory 交互可参考，但硬删除和弱 source-span 溯源不符合 NoName。

来源：[LangMem](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[Graphiti](https://github.com/getzep/graphiti)、[mem0](https://github.com/mem0ai/mem0)、[sqlite-vec](https://github.com/asg017/sqlite-vec)。

这些借鉴只解决存储语义和管线边界；去重、写入防中毒、时序冲突和检索质量仍需单独验证。
