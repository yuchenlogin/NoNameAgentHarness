# Harness 运行时骨架

> 本文为 NoName 的「工程兜底」：理念决定方向，稳定接口保证它能成为完整且可持续演进的 agent harness。

## 1. 模块地图

```
UI / TUI
  │
Application API
  ├── Session Service
  ├── Memory / Taste / Ledger Service
  ├── Agent Service
  └── Approval Service
        │
Agent Runtime
  ├── Router
  ├── Context Assembler
  ├── Model Recipe Resolver
  ├── Agent Loop
  └── Tool Executor
        │
Capability Layer
  ├── Model Adapters
  ├── Tool Registry
  ├── Plugin Runtime
  ├── Execution World / Sandbox
  └── Storage
```

## 2. Session Log

会话采用 append-only 事件流。消息历史、UI 时间线、账本和回放都从事件派生，不单独维护多份互相漂移的真相。

核心不变式：**模型可见的内容必须已经入日志，并能从日志重建。**

基础事件类型：

- `user.message`
- `context.assembled`
- `route.selected`
- `model.requested / model.chunk / model.completed / model.failed`
- `tool.requested / tool.approved / tool.completed / tool.failed`
- `artifact.changed`
- `memory.proposed / memory.reviewed / memory.superseded`
- `taste.card.generated / taste.reviewed`
- `session.compacted / session.forked / session.completed`

事件 schema 必须有版本号和迁移器。NoName 可以在 pre-release 阶段快速演化，但长期数据格式不能靠「清库重来」。

## 3. Model Adapter

适配器把供应商差异收敛成稳定能力：

- 消息与多模态内容格式；
- 工具调用和结构化输出；
- 流式事件；
- context window、输出上限、缓存能力；
- 推理、视觉、图像生成、embedding 等 capability；
- 价格、延迟、区域与隐私属性；
- 错误分类、重试和取消。

业务逻辑只能按 capability 选择模型，不直接依赖某家 API 字段。适配器必须保留供应商原始响应引用，便于审计。

## 4. Model Recipe

一个 recipe 是角色组合，不是模型列表：

```yaml
id: code-change-balanced
roles:
  planner:
    capability: reasoning
    budget: medium
  worker:
    capability: coding-tools
    budget: high
  critic:
    capability: independent-review
    different_family_from: worker
fallback: code-change-economy
```

建议的默认配方：

| 任务 | 默认角色 |
|---|---|
| 小型问答 | responder |
| 复杂研究 | planner → parallel researchers → synthesizer |
| 代码修改 | planner → worker → critic |
| 记忆写入 | extractor → conflict checker → human review |
| 品味卡生成 | clusterer → visualizer → human review |
| 高风险操作 | planner → policy checker → human approval → executor |

用户可以按任务类别替换任何角色。路由器记录推荐理由、覆盖原因、成本与结果反馈。

## 5. Tool Registry

工具注册必须分离：

- **模型可见面**：名称、说明、输入 schema、输出契约；
- **宿主执行面**：实现、超时、并发安全、权限、审批策略、展示方式。

执行管线：

```
validate → policy → approval → execute → normalize → log → present
```

工具注册有作用域：global、agent、session。局部工具可以遮蔽全局工具，但必须在账本中可见。工具卸载后所有监听、定时器和资源都要释放。

## 6. Agent Loop

Agent loop 建议采用显式状态机：

```
IDLE
 → ASSEMBLING_CONTEXT
 → SELECTING_MODEL
 → CALLING_MODEL
 → WAITING_TOOL / STREAMING_OUTPUT
 → APPLYING_RESULT
 → CHECKING_STOP
 → COMPACTING / COMPLETED / FAILED / CANCELLED
```

每次状态变化都是 session event。loop 只负责驱动，不承担模型路由、记忆写入或权限判断；这些通过服务接口完成。

停止条件必须明确：任务完成、用户暂停、轮次上限、预算上限、不可恢复错误、等待批准。恢复时从事件流重建状态，而不是依赖进程内对象。

## 7. Router 与 Context Assembler

Router 决定：继续当前上下文、fork、压缩后重生、切换 recipe、派生子 agent。它输出带理由的决定，不直接修改长期记忆。

Context Assembler 输入：任务、模型 capability、token 预算、法典投影、任务态、证据引用、品味投影、工具 schema、注入。输出必须被完整记录，以满足可回放。

## 8. Plugin Runtime

借鉴 DeepSeek Harness / Cordis：

- 依赖通过稳定 service key 声明；
- 插件贡献能力，而不是 import 具体实现；
- 每个副作用绑定生命周期并可逆；
- host 级服务与 agent 级贡献分离；
- capability seam 包含接口定义、provider、consumer；
- 同名能力可按作用域 shadow，但来源清晰。

NoName 的额外约束：

- manifest 声明权限、数据表、迁移、兼容版本和卸载行为；
- 插件不能直接修改 session event、memory 或 taste 表；必须经服务和 policy；
- 核心数据 schema 提供迁移承诺；
- 插件失败不能破坏证据日志；
- 插件只在工作流成熟后结晶，不把市场当新用户入口。

参考：[DeepSeek Harness architecture](https://github.com/deepseek-ai/DeepSeek-Harness/blob/HEAD/docs/architecture.md)。

## 9. 稳定核心与可替换部分

| 稳定核心契约 | 可替换实现 |
|---|---|
| Session Event schema + migration | SQLite / remote event store |
| Model Adapter protocol | 各供应商与本地模型 |
| Tool contract + policy pipeline | 文件、命令、浏览器等工具 |
| Memory/Taste versioning protocol | 提取器、检索器、embedding |
| Agent Loop state machine | 单 agent、多 agent、后台 agent |
| Plugin lifecycle | 具体 workflow 与领域能力 |
| Ledger query model | TUI、WebUI、多模态展示 |

长期主义不是冻结实现，而是冻结「可以安全替换实现的边界」。
