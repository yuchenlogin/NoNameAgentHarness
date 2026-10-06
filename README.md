# NoName Agent Harness

> 一个把「上下文」当作资产的 agent harness —— agent 与你的关系不是一次次会话，而是一本持续复利的账本。

**当前状态：本地优先的内核与运行时骨架已落地，无外部依赖（Python 标准库 + SQLite）。** 它已不只是验证跨会话接续的原型：证据/记忆内核、品味双轨与卡片、跨模型投影、工具审批门、Agent Loop、插件运行时、执行世界/沙箱、Router 都已实现，且每一层都经过对抗性测试。尚未接入真实外部模型（模型适配器是注入的契约，不是某个供应商的客户端）。

## 文档地图

| 文件 | 内容 |
|---|---|
| [docs/vision.md](docs/vision.md) | 愿景与核心理念：它是什么、为谁而做、核心原则（先读这个） |
| [docs/architecture.md](docs/architecture.md) | v0.2 总体架构：证据、记忆、品味、上下文包、插件、多模型与验证顺序 |
| [docs/memory-model.md](docs/memory-model.md) | 可溯源记忆：append-only 证据、source span、版本化失效、审核和检索投影 |
| [docs/runtime-architecture.md](docs/runtime-architecture.md) | 工程骨架：模型适配器、配方、工具注册表、会话日志、agent loop、插件、执行世界 |
| [docs/ledger.md](docs/ledger.md) | 交互式账本：时间线、因果图、状态 diff 和审核收件箱 |
| [docs/taste-cards.md](docs/taste-cards.md) | 品味双轨与多模态卡片：Authored Taste、Adopted Taste、复核与视觉风险 |
| [docs/prototype.md](docs/prototype.md) | 本地基座：SQLite 事件/证据、分层状态、候选审核、品味双轨、双时序与接续包 |
| [CHANGELOG.md](CHANGELOG.md) | 版本演进：每个版本改了什么、为什么这么做、有哪些已知边界 |
| [site/index.html](site/index.html) | 产品介绍页（manifesto）。单文件、无依赖，双击即可在浏览器打开 |

## 原则速览

1. **证据不可丢，记忆可投影** —— 压缩可以有损，原始证据必须可回放、可溯源。
2. **上下文包是视图，不是数据库** —— 每个模型和任务都可组装不同上下文，但指向同一份事实来源。
3. **规范与品味都必须审核** —— 法典写入是长期影响的签署行为；品味分为自述与采纳两条来源轨道。
4. **核心接口稳定，能力可以替换** —— 模型、工具、workflow 和插件都可演进，但事件、权限、版本和迁移有长期契约。
5. **确定性保障是物理不可能，不是模型不做** —— 沙箱、审批、账本是代码强制的边界。
6. **账本是地图，不是控制面板** —— 让人看见发生了什么、为什么发生、现在的状态从哪里来。

> 哪怕最后只有一个人在用，也愿意把它做出来。

## 已落地的能力

整个系统围绕一条主线运转：`append-only 事件/证据 → 候选审核 → 版本化分层状态 → 跨模型上下文包 → 新会话接管`。

| 层 | 能力 | 入口 |
|---|---|---|
| 证据/记忆 | append-only 事件与证据、候选提案、人工审核、版本化分层状态（高/中/低） | `event` `curate` `propose` `review` `state` |
| 双时序 | valid_from/valid_to 区分「事实何时为真」与「系统何时知道」，失效即版本化 | `review --valid-from/--valid-to` |
| 审核收件箱 | 待审法典、任务态、品味、卡片的统一聚合投影 | `inbox` |
| 记忆抽取 | 候选而非事实、提取器不自我确认、规则与 LLM 抽取器共用协议、fail-closed 幻觉防护 | `extract` |
| 语义检索 | FTS5 + 可重建向量投影、真语义 embedding 服务（复用凭证安全基类、可注入） | `embed search --semantic` |
| 品味 | Authored（自述即激活）/ Adopted（采纳需显式审核），只进独立 preference section | `taste-add` `taste-propose` `taste-review` `taste` |
| 品味卡片 | 确定性聚类、复核生命周期、图像视觉隐喻（抽象排版、可注入生成器）、确定性复核队列 | `card-propose` `card-create` `card-review` `card-queue` `card` `card-image` |
| 跨模型投影 | 同一事实基座为不同模型裁剪低层证据窗口，事实与状态溯源对所有模型一致 | `package --model-id/--budget/--task-type` |
| 配方 | 任务类型→角色链的建议与入账（可审计、可覆盖） | `recipes` |
| Router | 显式带理由的上下文路由（继续/fork/重生/切换配方），按调用配对的挂起审批与边界锚定的饱和度 | `route` |
| 工具注册表 | 模型可见面与宿主执行面分离，审批为账本支撑的一次性令牌（绑定参数哈希+session+工具实例代） | Python API `ToolRegistry` |
| Agent Loop | 显式状态机，工具调用路由过审批门，从事件流恢复 | Python API `AgentLoop` |
| 插件运行时 | 能力结晶的加载/校验/原子生命周期/审计，绝不绕过内核 | Python API `PluginRuntime` |
| 执行世界/沙箱 | 文件操作强制工作区约束 + O_NOFOLLOW，纯只读命令允许列表 + argv 路径扫描 + 进程组超时 | Python API `Sandbox` |
| 交互式账本 | 三视图（收件箱/状态/时间线）+ 渐进披露的离线 HTML | `ledger-html` |

完整命令、边界与已知限制见 [docs/prototype.md](docs/prototype.md)；每个版本的设计依据见 [CHANGELOG.md](CHANGELOG.md)。

## 快速开始

```bash
python3 -m noname_harness init --db .noname/harness.db --root .
pytest -q
```

## 尚未做（诚实边界）

- 真实外部模型调用的流式、计费与自动模型切换——真实供应商适配器（OpenAI/Anthropic）已以插件接入并经确定性 replay 端到端验证，但未经真实网络调用，且适配器是可选插件而非内核依赖；
- 语义重排——向量检索与真实 embedding 服务已落地（词面级默认嵌入 + 可重建投影，真语义服务可注入），文字检索为 FTS5；
- 真实图像模型插件待注入（品味卡片多模态视觉层已落地，默认渲染器为确定性抽象排版）；
- 网络隔离与资源限额（沙箱当前为文件边界 + 命令允许列表 + 进程组超时）；
- 暂停后 resume（恢复当前为只读重建终态，waiting_approval 后需开启新 run）。
