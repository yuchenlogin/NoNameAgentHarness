# NoName 本地最小原型

> 状态：本地 MVP · 2026-09-29（schema v4：品味双轨、双时序与审核收件箱）
>
> 这个原型只验证一条最窄的纵向切片：新会话能否在不重新解释项目边界的情况下，接续上一次工作的真实状态。

## 1. 这版原型做什么

原型提供一个无外部依赖的 Python + SQLite 本地事实基座：

```text
事件 / 证据
    ↓
整理 agent 的候选提案
    ↓
人工审核与版本化状态
    ↓
高层 + 中层 + 低层上下文包
    ↓
新会话接管
```

其中：

- **高层**保存项目目标、长期约束和已确认决策；
- **中层**保存当前阶段、任务进度、开放问题和下一步；
- **低层**保存最近的文件变化、测试结果、命令和错误等事件；
- 接续包明确携带工作区边界和“不得修改边界之外文件”的策略，因此新会话不必重复这类操作性提醒；
- 每条可进入高层或中层的状态都必须引用至少一个来源事件；
- 事件和证据只增不改，状态通过新的 revision 取代旧 revision；
- 上下文包同时有机器可读的 JSON 形式和人/模型可读的 Markdown 形式；Markdown 会保留低层证据的原文内容，并同时显示来源和 hash。
- 事件与证据可以通过 SQLite FTS5 搜索；索引可重建，永远不是事实来源。

这版的“整理 agent”是一个保守的服务契约，而不是真正的模型调用。它只接受事件中的结构化候选提示，先把提案、来源、冲突和审核链路跑通；未来的 LLM 整理器可以替换它，但不能绕过相同的存储与审核契约。

## 2. 快速开始

在项目根目录执行：

```bash
python3 -m noname_harness init --db .noname/harness.db --root . --name "我的项目"
```

记录一个项目约束和一次当前任务：

```bash
python3 -m noname_harness event \
  --db .noname/harness.db \
  --session s1 \
  --type project.constraint \
  --payload '{"key":"workspace_boundary","content":{"text":"只能修改项目根目录内的内容"}}'

python3 -m noname_harness event \
  --db .noname/harness.db \
  --session s1 \
  --type task.updated \
  --payload '{"key":"current_task","content":{"goal":"修复算法测试问题","progress":"已复现","next":"检查最近 diff"}}'
```

在会话结束或准备切换 agent 前，可以显式记录一次只读工作区快照；生成接续包时系统也会自动做同样的快照：

```bash
python3 -m noname_harness snapshot --db .noname/harness.db --session s1
```

如果项目使用 git，快照会包含当前 HEAD 和未提交状态；它只执行 `git status` / `git rev-parse`，不会修改仓库。

让整理服务把结构化提示变成候选：

```bash
python3 -m noname_harness curate --db .noname/harness.db --session s1
```

也可以在事件中显式附带候选，例如：

```json
{
  "candidate": {
    "layer": "mid",
    "kind": "task-state",
    "key": "current_task",
    "content": {
      "goal": "修复算法测试问题",
      "progress": "已复现",
      "next": "检查最近 diff"
    },
    "confidence": 0.92,
    "reason": "用户反复强调该任务是当前阶段的主线"
  }
}
```

查看候选并审核（把命令输出的 proposal id 代入）：

```bash
python3 -m noname_harness proposals --db .noname/harness.db
python3 -m noname_harness state --db .noname/harness.db
python3 -m noname_harness review \
  --db .noname/harness.db \
  --proposal-id prp_... \
  --action accept \
  --reviewer user
```

生成新的接续包：

```bash
python3 -m noname_harness package \
  --db .noname/harness.db \
  --session s1 \
  --task "继续处理上次的测试问题" \
  --out .noname/packages/handoff.md
```

默认输出的 Markdown 是给人或新模型阅读的视图；同一份上下文也可以用 `--format json` 输出为结构化包。

需要从历史里找回某个失败原因或改动时，可以搜索事件和证据：

```bash
python3 -m noname_harness search "empty input" --db .noname/harness.db --session s1
```

检查事件与证据的 hash，或在需要时重建文字索引：

```bash
python3 -m noname_harness verify --db .noname/harness.db
python3 -m noname_harness reindex --db .noname/harness.db
```

### 2.1 品味双轨：自述与采纳

品味在这个原型里是独立的一等公民：有自己的表、自己的审核动作、自己的上下文分区。它与事实严格隔离——品味只影响态度（方案排序、表达风格、取舍偏好），绝不作为任何事实主张的证据。

两条来源轨道有不同的激活门槛：

- **Authored（自述）**：用户直接写下的态度。写下即确认，因此立即激活，权威最高；来源事件可选（用户本人就是来源），附上只是让溯源更完整。
- **Adopted（采纳）**：从一次模型回答、作品或涌现时刻中提炼的倾向。必须引用至少一个来源事件，且始终以 `candidate` 身份进入，只有经过显式 `adopt` 审核才会激活——采纳品味不能伪装成用户原话。

记录一条自述品味（内容应写具体例子和判断，而不是形容词）：

```bash
python3 -m noname_harness taste-add \
  --db .noname/harness.db \
  --scope project \
  --content '{"attitude":"在工作工具里偏好克制、信息密度高的设计","example":"状态输出先给结论，再给证据","avoid":"装饰性面板"}' \
  --reason "多次评审中用户反复这样取舍"
```

从模型时刻提出一条采纳候选（必须引用来源事件，事件 id 可从 `ledger` 或 `search` 输出取得）：

```bash
python3 -m noname_harness taste-propose \
  --db .noname/harness.db \
  --content '{"attitude":"解释失败原因时先还原现场，再给结论","example":"按时间线列出命令、输出和推断"}' \
  --source-event evt_... \
  --reason "模型 s1 的排障回答让用户眼前一亮"
```

审核品味记录。生命周期动作是 `adopt / edit / pause / resume / retire`；每次审核都写入不可变的审核记录，`edit` 产生一条取代旧记录的新版本，旧版本仍可追踪：

```bash
python3 -m noname_harness taste-review \
  --db .noname/harness.db \
  --taste-id tst_... \
  --action adopt \
  --reviewer user \
  --reason "确认这条倾向代表我当前的偏好"

python3 -m noname_harness taste-review \
  --db .noname/harness.db \
  --taste-id tst_... \
  --action pause \
  --reviewer user
```

列出活跃品味（默认）或待审候选：

```bash
python3 -m noname_harness taste --db .noname/harness.db
python3 -m noname_harness taste --db .noname/harness.db --status candidate
python3 -m noname_harness taste --db .noname/harness.db --scope project
```

生成接续包时，活跃品味进入独立的 `preference` section，明确标注 `influence: soft` 并附带约束说明（不得改写事实、不得降低验证标准、不得覆盖任务要求），provenance 中单独记录 `taste_ids`。它不进入高、中、低任何一个事实层。

### 2.2 双时序与审核收件箱

一条状态有两个不同的时间：**valid time** 是事实在世界上何时为真，**recorded time** 是系统何时知道。原型只自动记录后者；前者由审核人在接受候选时显式声明：

```bash
python3 -m noname_harness review \
  --db .noname/harness.db \
  --proposal-id prp_... \
  --action accept \
  --reviewer user \
  --valid-from 2026-09-01T00:00:00+00:00 \
  --valid-to 2026-12-31T23:59:59+00:00
```

`--valid-to` 不能早于 `--valid-from`，存储层会拒绝。用 `--action retire` 让一条状态失效时，同样可以用 `--valid-to` 记录它在世界上何时停止为真——失效是写入新的版本化 revision，不是删除历史。

`inbox` 命令对应 docs/ledger.md 的「审核收件箱」：一个聚合投影，把等待人工判断的事项收拢成一张高价值待办清单，而不是原始事件流：

```bash
python3 -m noname_harness inbox --db .noname/harness.db
```

输出按影响范围分组：

- `canon_pending`：待审法典候选（高层，长期影响，逐条确认）；
- `task_pending`：待审任务态候选（中层）；
- `taste_pending`：待审品味候选（adopted 轨道，附来源事件与提出理由）；
- `counts`：各类数量汇总。

每条事项附带来源事件、冲突引用和提出理由，让审核人做判断而不是盲点。收件箱完全由 append-only 表派生，自身不存任何事实，随时可以重建。

## 3. 当前不做什么

这不是完整的 agent runtime，目前明确不包含：

- 外部模型调用、模型适配器和自动模型切换；
- 文件、命令、网络工具的实际执行；
- 自然语言记忆抽取、向量检索和语义重排（当前只有 FTS5 文字检索）；
- 品味卡片、聚类、多模态图像和插件运行时（品味层本身已有双轨 MVP：两条来源轨道、版本化审核、独立 preference section，但无卡片、无视觉、无聚类）；
- 多用户同步、远程数据库和加密存储。

低层目前是“最近工作事件窗口”（`low_limit` 只限制这些工作事件），并在指定会话时额外附带一次当前工作区快照；它还不是最终的动态语义检索。这样做是有意的：先验证证据、审核、分层和接管契约，再逐步替换投影实现。

## 4. 已验证的不变量

测试覆盖以下行为：

1. 事件和证据无法通过 SQL 更新或删除；
2. 长期状态候选没有来源事件时不能创建；
3. 接受或编辑候选会产生新的不可变 revision，旧 revision 仍可追踪；
4. 整理服务不会把普通测试失败自动提升为长期状态；
5. 上下文包同时暴露高、中、低三层和 provenance；
6. 审核事件会记录生成的 revision 及其 superseded 关系；
7. 证据引用和上下文包输出不能越过配置的工作区边界；
8. 品味与事实隔离：品味只进入上下文包的独立 `preference` section，不进入高、中、低任何一个事实层，也不能作为事实提案的来源；
9. adopted 品味必须引用至少一个来源事件，且只有显式 `adopt` 审核才能激活；观察到的倾向未经审核永远只是 `candidate`；
10. append-only 与版本化在品味层同样成立：品味记录和审核记录不可改不可删，`edit` 写入取代旧版本的新记录；
11. 双时序边界受校验：`valid_to` 不得早于 `valid_from`，retire 通过版本化失效而非删除；
12. 审核收件箱是聚合投影：它由 append-only 表派生，自身不存事实，可随时重建。

## 5. 下一步验证

最重要的不是增加更多命令，而是用一个真实项目做一次“重生测试”：

1. 在旧会话中记录一次算法改动、测试失败和当前进度；
2. 生成 Markdown 接续包；
3. 在全新会话中只提供接续包和一句新的任务指令；
4. 检查新 agent 是否能准确回答：当前目标是什么、已经尝试过什么、问题在哪里、下一步是什么、哪些边界不能碰。

如果这一步失败，应先修正分层和投影规则，而不是继续添加模型、插件或 UI 功能。

## 6. 已知限制与安全边界

- 当前 SQLite 会保存传入的原始内容，没有加密和敏感信息脱敏；只应在本地、可信的项目环境中试用。
- 当前长期状态的溯源粒度是 `source_event_ids`；完整的字符/消息区间 `source_span` 和跨作用域记忆仍属于后续 schema，不应把这个 MVP 误认为最终记忆系统。
- 事件 schema 目前只有基础版本迁移（包含提案理由字段）；正式长期使用前仍必须补上更完整的迁移测试。
- `curate` 的结构化输入是临时替身，不代表自然语言分类问题已经解决。
- 数据库触发器保护核心表，但用户仍应把数据库本身纳入备份和访问控制范围。
