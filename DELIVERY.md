# NoName Agent Harness · 交付说明

> 2026-10-07 · schema v7 · 355 测试全绿 · 无外部依赖（Python 标准库 + SQLite）

这份文档对照 vision 的原始预期，逐项核验 NoName 当前状态的证据。它不是营销材料，而是一份可审计的自证：每一条"已落地"都附对应的模块与测试，每一条"未做"都诚实标注。

## 一句话

NoName Agent Harness 是一个把「上下文」当作资产的 agent harness：agent 与你的关系不是一次次会话，而是一本持续复利的账本。现在，这本账本的**内核、运行时骨架、品味层、检索层、真实模型接入已全部落地**。

## 验证方式（怎么证明没有 bug）

- **355 个自动化测试全绿**（`pytest -q`），覆盖每个模块的 happy path 与失败路径。
- **二十二轮对抗性审查**：每个安全关键层（审批门、沙箱、凭证安全、投影、抽取、检索、图像、账本）都经过独立 reviewer 用可执行探针攻击，发现的每个 high/medium 漏洞都已修复并配回归测试。沙箱与 OpenAI 适配器各经历两轮 REJECT 级专攻后才通过。
- **一次系统性交付审计**：vision 原则、runtime-arch 稳定接口、文档一致性三路并行核对，发现的偏差已全部修复。
- **系统级综合验证**：10+ 项核心能力在一个真实工作流中协同验证（见文末）。

## vision 四条原则 · 逐项核验

| 原则 | 状态 | 证据 |
|---|---|---|
| **一、上下文是资产** | ✅ 已证实 | append-only 事件/证据（全表 UPDATE+DELETE 触发器物理强制）、分层投影（高/中/低）、跨模型接续包（`assemble_context_package`）、双时序（valid_from/valid_to）。测试：`test_prototype.py`、`test_rebirth.py`、`test_model_projection.py`、`test_memory_semantics.py` |
| **二、规范即记忆** | ✅ 已证实 | 法典由模型观察后提出（`extractor.py` 规则替身 + `llm_extractor.py` LLM 抽取）、由人罕见批准（`review_proposal`）、版本化可回滚（supersedes 链 + bitemporal）。提取器**绝不写长期状态、绝不自我确认**——候选而非事实。测试：`test_extractor.py`、`test_llm_extractor.py` |
| **三、核心不可谈判，能力可以涌现** | ✅ 已证实 | 审批门（`tools.py` 账本支撑的一次性令牌，绑定参数哈希+session+工具实例代）、沙箱（`sandbox.py` 工作区约束+纯只读命令允许列表+argv 路径扫描+进程组超时，两层独立防线）。物理强制，非模型不做。测试：`test_tools.py`、`test_sandbox.py`、`test_cross_module_hardening.py` |
| **四、界面是一张地图，不是控制面板** | ✅ 已证实 | 交互式账本（`ledger_view.py` 单文件离线 HTML：审核收件箱/状态/版本演进/因果图/时间线五视图 + `<details>` 渐进披露），纯投影可重建，克制暗色设计（与 site 一致）。测试：`test_ledger_view.py`，并经 Chrome headless 截图核验 |

## vision §12 五步验证顺序 · 全部落地

1. ✅ **append-only 证据、source span、分层状态、记忆候选、人工审核** — `store.py` + `curator.py`
2. ✅ **上下文包投影与跨模型接管** — `assemble_context_package`（事实/状态溯源对所有模型一致）+ 重生测试
3. ✅ **模型配方、工具注册、账本因果视图** — `recipes.py`、`tools.py`、`ledger_view.py`
4. ✅ **品味双轨与卡片复核** — `taste.py`（Authored/Adopted）、`taste_cards.py`、`card_images.py`
5. ✅ **插件结晶与多模态视觉层** — `plugins.py`、真实供应商适配器（OpenAI/Anthropic）、卡片图像

## runtime-arch 稳定接口 · 全部落地

| 接口 | 模块 | 状态 |
|---|---|---|
| Session Log（append-only、迁移） | `store.py`（schema v7，v1→v7 幂等迁移链） | ✅ |
| Model Adapter（契约 + 两个真实供应商） | `adapters.py`、`openai_adapter.py`、`anthropic_adapter.py`、`vendor_http.py` | ✅ |
| Model Recipe（任务类型→角色链） | `recipes.py` | ✅ |
| Tool Registry（审批门） | `tools.py` | ✅ |
| Agent Loop（状态机 + 恢复） | `agent_loop.py` | ✅ |
| Router（显式带理由路由） | `router.py` | ✅ |
| Plugin Runtime（能力结晶） | `plugins.py` | ✅ |
| Execution World / Sandbox | `sandbox.py` | ✅ |

## 品味（一等公民）· 完整

- **双轨**：Authored（自述即激活，最高权威）/ Adopted（采纳需显式审核，不伪装成用户原话）。
- **卡片**：确定性聚类、生命周期状态机（含原子 split）、确定性复核队列、stale 标注。
- **图像视觉隐喻**：可注入 `ImageGenerator` 协议 + 确定性抽象排版渲染器（无人脸/摄影/敏感元素）；图像字节作为 append-only evidence span 原子持久化可溯源；多模态风险防控内建（视觉解释标注、生成器只收文本、abstract/no_faces 来自生成器）。

## 检索 · 完整

- **FTS5**（默认文字检索，可重建索引）。
- **向量投影**（可重建，`event_embeddings`，schema v7）：词面级 `local_hash_embedding`（默认，无网络可验证）。
- **真语义 embedding 服务**：`OpenAIEmbedding`（复用 `vendor_http` 凭证安全基类），真语义召回（如英文查询召回中文相关事件）。
- **重排已落地**：`rerank.py`（`RerankFn` 协议 + `default_rerank` 确定性多维评分），三阶段（召回 / 重排 / 构造）闭环，每条结果带可解释 `rerank_reasons`。

## 真实模型接入 · 协议经多供应商验证

- **ModelAdapter 契约**：业务逻辑只按 capability 选模型，不依赖供应商字段。
- **两个真实供应商适配器**（OpenAI + Anthropic），共享 `vendor_http` 凭证安全基类（无重定向、HTTPS 强制、vendor_ref 白名单、错误按因分类、api_key repr=False）。
- **协议通用性实证**：同一 `AdapterDriver` + `AgentLoop`，仅替换适配器实例即可驱动两家完成完整多轮工具循环，业务逻辑零改动。
- **真实能力以插件接入**：模型适配器、embedding 服务、（未来的图像模型）都按"能力结晶成插件"加载审计，内核不依赖任何供应商。
- **真流式 SSE 已落地**：`secure_stream_transport`（逐行读取、无重定向、HTTPS 强制、错误按因分类）+ `iter_sse_json_lines` 解析；OpenAI 按 index 累积 tool_call 片段、Anthropic 按事件类型解析并 flush orphan blocks；默认真实流式，显式 `None` 回退 complete 重放 replay。

## CLI · 29 个命令

`init event snapshot curate extract propose review state proposals inbox ledger ledger-html search embed package recipes route taste-add taste-propose taste-review taste card-propose card-create card-review card-queue card-image card verify reindex`

（运行 `python3 -m noname_harness <command> --help` 查看每个命令的参数。）

## 尚未做（诚实边界）

这些都是已明确记录的增强项或更深的系统层，**不是核心缺口**：

- **真实图像模型插件**：品味卡片默认抽象排版渲染器已落地，真实 imagegen 插件待注入。
- **取消机制**：`cancelled` 错误分类已有，无 cancel API。
- **多模态消息格式**：`ModelMessage.content` 仅文本。
- **并行 tool_call**：Agent Loop 单 tool_call/轮，并行响亮拒绝。
- **暂停后 resume**：恢复为只读重建终态，waiting_approval 后需开启新 run。
- **网络隔离与资源限额**：沙箱当前为文件边界 + 命令允许列表 + 进程组超时。
- **多用户同步、远程数据库、加密存储**。

## 系统级综合验证（2026-10-07 实测）

10+ 项核心能力在一个真实工作流中协同验证，全部通过：证据/记忆+双时序、记忆抽取（提案非 active）、审核成法典、审核收件箱、品味+卡片、跨模型投影一致、沙箱读免审、写拦截（审批门）、AgentLoop+恢复、Router、账本 UI、完整性校验。

## 快速开始

```bash
python3 -m noname_harness init --db .noname/harness.db --root . --name "我的项目"
pytest -q   # 381 passed
```

## 演进

22 个版本（0.3.0 → 0.23.0），每个版本的 Features / Design Rationale / Notes & Caveats 见 [CHANGELOG.md](CHANGELOG.md)。设计文档见 [docs/](docs/)（vision、architecture、memory-model、runtime-architecture、ledger、taste-cards、prototype）。

---

> 哪怕最后只有一个人在用，也愿意把它做出来。——它做出来了。
