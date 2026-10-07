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
- `workspace.snapshot`
- `memory.proposed / memory.reviewed / memory.superseded`
- `attention.requested / handoff.next_step_suggested`
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

**原型落地状态**：契约已落地——`ModelAdapter` 协议（`id` / `capability` / `complete` / `stream` / `estimate_cost`），统一数据类型 `ModelMessage` / `ModelRequest` / `ModelResponse` / `StreamEvent`（`vendor_ref` 保留供应商原始响应引用），统一错误分类（`rate_limit` / `timeout` / `overloaded` / `auth` / `invalid_request` / `cancelled` / `unknown`）+ `retryable` 标注。`LocalEchoAdapter` 是确定性、无网络的参考实现（非 vendor mock），用于验证契约并驱动 Agent Loop。`AdapterDriver` 把适配器包装为 loop 的 `SessionDriver`：组装上下文时注入 `tool_registry.visible_tools`，跨模型边界的 `approval_token` 经 `registry.get_live_token` 重新水合，`ModelAdapterError` 的 `error_class` / `retryable` / `vendor_ref` 在失败事件与 summary 中保留入账。真实供应商适配器（OpenAI / Anthropic / 本地模型）按同一协议以插件接入，内核不依赖任何供应商。多模态消息格式已落地：`TextBlock` / `ImageBlock` / `ContentBlock`，`ModelMessage.content` 支持 `str | list`（纯文本向后兼容）；[OI] / Anthropic 各自的 content-part / block 映射；`vision` 构造字段 + `vision=False` 统一拒绝多模态且不可按 role 绕过——capability 声明与格式能力一致。

第一个真实供应商适配器已落地：`noname_harness/openai_adapter.py`（`OpenAIAdapter` + `load_openai_adapter`），支持 OpenAI 兼容端点。要点：传输层可注入（默认真实 urllib POST，测试用确定性 replay 传输，无网络/key 即可验证请求构建、错误分类、`vendor_ref`）；base URL scheme 归一化强制 HTTPS（`allow_insecure` 仅本地 opt-in）；拒绝一切重定向（防 302 转发 Bearer key）；`vendor_ref` 全路径白名单——只存 `{status, id, error_code, token usage}`，错误体永不落盘、永不放 bytes；错误按因分类（true timeout 可重试，DNS/连接/TLS 不可重试）；`PluginManifest.side_effects` 声明 `(network-egress, billing)` 记 `plugin.loaded` 审计。该适配器经插件加载、驱动 Agent Loop 完成端到端验证（replay 无网络），「能力结晶成插件」从原则变为已验证事实。

第二个真实供应商适配器已落地：`noname_harness/anthropic_adapter.py`（`AnthropicAdapter` + `load_anthropic_adapter`），与 OpenAI 同一契约，只写 Messages API 差异（`/messages` 端点、`x-api-key` + `anthropic-version` 头、`system` 顶层字段、content 块数组、`tool_use` 块、`stop_reason`、usage 字段名）。两家适配器共用 `noname_harness/vendor_http.py` 凭证安全基类（无重定向、HTTPS 校验、`vendor_ref` 白名单、错误分类、成本/JSON Schema 助手），硬赢的保障集中一处不随供应商漂移。协议通用性已实证：同一 `AdapterDriver` + `AgentLoop` 仅替换适配器实例，即可驱动两家完成完整多轮工具循环（请求工具 → 回传结果 → 回答），业务逻辑零改动。跨供应商映射要点：`role="tool"` 映射为 user + `tool_result` 块（带 `tool_use_id`，driver 以 `_last_tool_call_id` 穿线关联）；`finish_reason` 归一化（`tool_use`→`tool_calls` 等）；`error_code` 限 snake_case 枚举白名单，防凭证随错误体回显。

真流式 SSE 已落地：`secure_stream_transport` 逐行读取 + `iter_sse_json_lines` 解析；OpenAI/Anthropic 各自处理分片/orphan 场景（OpenAI 按 index 累积 tool_call arguments 片段、Anthropic 按事件类型解析并 flush orphan blocks）；默认真实流式，显式 `None` 回退 complete 重放 replay。

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

### 原型落地状态

`noname_harness/tools.py` 已实现本节核心（schema v5）：

- 模型可见面（`ToolSchema`）与宿主执行面（`Tool`）已分离，`visible_tools` 只暴露契约；
- 审批为账本支撑的一次性令牌：`grant_approval` 铸造并记 `tool.approval_granted`，绑定参数哈希与 session、单次使用，未消费令牌可从账本重建；
- 遮蔽单调性 + tombstone：同名注册与 unregister 后重注册都不能削弱审批门；
- 作用域保留 global / session 两级，agent 因原型层无强制力暂移除；
- 落地管线为 `validate → approval → execute → log → return`（policy / normalize / present 在原型层尚无对应物）；
- 执行世界/沙箱已落地：`noname_harness/sandbox.py` 提供文件读/写与命令执行——文件操作强制约束在工作区根目录内，写入用未 resolve 原始路径 + `O_NOFOLLOW` 关闭 TOCTOU 窗口（resolve 仅用于边界判定，平台缺 `O_NOFOLLOW` 则 fail-closed）；命令执行为纯只读默认允许列表（`git`/`find`/解释器等任意执行原语全部移除，需显式 opt-in）+ argv 路径扫描（工作区外路径参数直接拒绝）+ 独立进程组硬超时（`killpg` 杀整组）。产出注册进 ToolRegistry 的工具走审批门（`read_file`=never、`write_file`=always、`run_command`=destructive/always）——沙箱边界与审批门是两层独立防线，每次操作产出证据事件。本层不含网络隔离与资源限额（CPU/内存），属更深的沙箱层。

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

### 原型落地状态

`noname_harness/agent_loop.py` 已实现本节核心（schema v5）：

- 状态机已落地，含合法转移表，非法转移即 `AgentLoopError`；`STREAMING_OUTPUT` / `COMPACTING` 因原型层无驱动可达暂未实现，待真实驱动接入后恢复；
- 每次状态转移都是 `loop.transition` 事件，转移全入账；停止条件即本节所列六项；
- 驱动异常与契约违反（矛盾 `LoopResult`、未知 `stop_reason`）统一归一为 `FAILED`，经 `_force_fail` 写真实 transition（`forced: true`）到终态；
- `reconstruct()` 从事件流重建状态 / 轮次 / limits（从 `loop.started` 读回），不依赖进程内对象；
- driver 为注入的 `SessionDriver` 协议：原型用确定性 stub，生产接 Model Adapter；loop 不路由模型、不写记忆、不判权限，复用 `assemble_context_package` / `resolve_recipe` / `ToolRegistry`。
- 取消机制已落地：`AgentLoop.cancel` 事件驱动（`loop.cancel_requested` append-only 事件，任何 actor 可从 loop 线程/进程外发起）、轮次边界协作式检测（`_check_stop` 转为 `CANCELLED`，不中断阻塞中的 `driver.act` 但不再开始下一轮）、规则 `cancel.seq > MAX(loop.finished.seq)`（新 cancel 正确归属下一个 run）、CLI `cancel --session [--reason]`，跨连接（外部 actor 独立 store）端到端验证。
- 恢复机制已落地：`AgentLoop.resume()` 续跑 waiting_approval 暂停的 run——三道门（gate 1：最近一次 `loop.finished` 须为 waiting_approval；gate 2：**每次暂停只能 resume 一次**，`loop.resumed` 只 veto 它对应的那次暂停，seq 作用域与 `_cancellation_requested` 同构，pause→resume→pause→resume 合法循环可行；gate 3：令牌须为绑定 pending 调用的 live 一次性令牌，校验不消费）。轮次继承按 run 隔离：只统计最近一次 `loop.started` 之后的 transition 轮次，同 session 早先 run 不消耗本 run 预算；`max_rounds`/`budget_rounds` 跨暂停持续绑定，resume 不是预算后门。
  - **并发安全**：gate 1+2 的检查与 `loop.resumed` 认领写入在同一个 `BEGIN IMMEDIATE` 事务内完成（check 与 claim 原子化），并发 actor 竞速时第二个在写锁上阻塞后重读账本被 gate 2 拒绝——不存在 check-then-act 双跑窗口；令牌的一次性消费仍由注册表执行期仲裁兜底。
  - **信任边界（明说）**：事件流无作者概念，resume 信任账本中记录的 `loop.finished`/`loop.started` 内容——**能写 session 事件流的 actor 已被信任**。伪造 finish 事件只能解锁 resume 之门，不能伪造审批令牌（gate 3 校验的是注册表 live grant，不是事件内容），也不能绕过执行期的工具名/参数哈希/session 实名绑定；审批门的物理防线是 gate 3 + 执行期校验，而非事件谓词。`_drive` 私有入口另有纵深防御断言（仅 IDLE 或 resume 显式置位的 CANCELLED+scratch 可入），但它不是安全边界。

## 7. Router 与 Context Assembler

Router 决定：继续当前上下文、fork、压缩后重生、切换 recipe、派生子 agent。它输出带理由的决定，不直接修改长期记忆。

Context Assembler 输入：任务、模型 capability、token 预算、法典投影、任务态、证据引用、品味投影、工具 schema、注入。输出必须被完整记录，以满足可回放。

### 7.1 State Curator：状态整理器

状态整理器是一个独立的角色，不等同于执行任务的 worker。它消费 session event 和 artifact 指针，负责：

1. 按时间尺度和作用域整理高层、中层候选；
2. 记录来源、置信度、冲突和“为什么值得升级”；
3. 在发现旧状态与新证据不一致时提出冲突，而不是静默覆盖；
4. 只在需要用户判断时发出轻量注意力请求；
5. 生成版本 diff，等待用户或独立审核者批准。

整理器可以由模型实现，但“提案”和“批准”必须是两个可审计的动作。当前本地原型用结构化事件提示替代模型整理，先验证这条契约。

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
