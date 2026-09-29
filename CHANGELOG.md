# Changelog

> 只记录版本级变化：新功能、重大重构、架构调整、破坏性变更。不是每个 commit 都有条目。


## [0.15.0] - 2026-09-29

### Features

- 第一个真实供应商适配器落地（runtime-arch §3，经两轮对抗性审查——两次 REJECT 后修复，均为凭证安全——schema 不变，仍为 v6）：新增 `noname_harness/openai_adapter.py`（`OpenAIAdapter` + `load_openai_adapter`），与 `LocalEchoAdapter` 同一 `ModelAdapter` 契约的另一实现，支持 OpenAI 兼容端点；API key 从环境变量读取、只用于请求头。验证「能力结晶成插件」：真实供应商以插件接入，内核不依赖任何供应商。
- 传输层可注入：默认真实 urllib POST，测试用确定性 replay 传输——无需网络/key 即可验证请求构建、vendor schema 映射、错误分类、`vendor_ref`。
- 凭证安全加固：`_NoRedirectHandler` 拒绝一切重定向（否则 302 会把 Bearer key 转发到攻击者主机——silent 凭证外泄）；base URL scheme 归一化强制 HTTPS（plaintext / `HTTP://` 大小写 / `ftp://` 全拒），`allow_insecure` 仅本地端点 opt-in。
- `vendor_ref` 全路径白名单：只存 `{status, id, error_code, 三个 token usage 字段}`，错误体永不落盘（OpenAI auth 错误常含 `Bearer sk-...`），永不放 bytes（否则 `append_event` 崩溃、loop 卡非终态）。
- 错误按因分类：true timeout（含 URLError 包裹）→ `timeout` 可重试；DNS / 连接 / TLS → `overloaded` 不可重试。
- `PluginManifest` 新增 `side_effects` 字段，OpenAI 插件声明 `(network-egress, billing)` 记 `plugin.loaded`（manifest 的 `max_permission` 只覆盖贡献的工具，此插件不贡献工具但做网络出站+计费，必须诚实声明）。
- 边界加固：dict tool_call arguments 直传、非法 JSON 归一化为 `ModelAdapterError`、超 1MB 拒绝。
- 端到端验证：插件加载 → adapter 驱动 Agent Loop → 模型见 3 个沙箱工具契约 → `read_file` 过审批门 → 读真实文件 → 返回中文答案 → 账本可审计（全部 replay 无网络）。

### Design Rationale

- **为什么传输层必须可注入**：请求构建/响应解析与实际 HTTP 必须分离——契约正确性（请求构建、错误分类、`vendor_ref`）需要在无网络、无 key 的环境下用确定性 replay 传输验证，默认真实 urllib 只在部署时启用。否则验证契约就要拖真实网络与真实凭证进场，凭证安全的每一轮加固都无法在测试中复现。
- **为什么 `vendor_ref` 必须全路径白名单、永不放错误体/bytes**：错误体可能含 API key 或攻击者控制的内容，一旦入账本即凭证外泄——审计面就是泄密面；bytes 不可 JSON 序列化，会让 `append_event` 崩溃、loop 卡非终态。只存 `{status, id, error_code, token usage}` 让审计可用而泄密不可能：可审计性不依赖存下供应商说的一切，只依赖存下足以定位真相的引用。

### Notes & Caveats

- 默认只允许 HTTPS；`allow_insecure` 仅为本地端点 opt-in。
- 真实网络调用未在测试中启用（replay 验证契约，部署时启用真实 urllib）。
- `side_effects` 是声明性审计字段，不是强制门——诚实声明靠插件作者，账本只负责记录。
- `estimate_cost` 是词数估算，非真实 token 计数。
- 新增 28 个测试，总数到 241。

## [0.14.0] - 2026-09-29

### Features

- Model Adapter 契约 + Agent Loop 桥接落地（runtime-arch §3，经一轮对抗性审查加固，schema 不变，仍为 v6）：新增 `noname_harness/adapters.py`——`ModelAdapter` 协议（`id` / `capability` / `complete` / `stream` / `estimate_cost`）；统一数据类型 `ModelMessage` / `ModelRequest` / `ModelResponse` / `StreamEvent`，`vendor_ref` 保留供应商原始响应引用；统一错误分类（`rate_limit` / `timeout` / `overloaded` / `auth` / `invalid_request` / `cancelled` / `unknown`）+ `retryable` 标注。
- 参考实现 `LocalEchoAdapter`：确定性、无网络（非 vendor mock），完整实现契约，`responder` 可注入规则，用于验证契约并驱动 Agent Loop；真实供应商适配器（OpenAI / Anthropic / 本地模型）按同一协议以插件接入，内核不依赖任何供应商。
- `AdapterDriver`：把 `ModelAdapter` 包装成 Agent Loop 的 `SessionDriver`——从上下文包构建 `ModelRequest`（brief 含 high / mid / low / guardrails / next_steps / preference + visible_tools 契约不含实现），调用适配器，把 `tool_call` 映射为 `LoopResult` 交给 loop 路由过审批门。
- 审查加固：Agent Loop 组装上下文时注入 `tool_registry.visible_tools`（修复「真实路径模型看不到工具契约」）；`registry.get_live_token(id)` 重新水合跨模型边界的 `approval_token`（修复「token 类型腐坏致 resume 崩溃」）；`ModelAdapterError` 的 `error_class` / `retryable` / `vendor_ref` 在 loop 失败事件与 summary 中保留（不再被字符串化擦除）；tool_call 边界校验（无 name / 并行调用 / arguments=null 响亮拒绝）。
- 端到端验证：真实 adapter 驱动的完整 agent run（模型读文件免审 → 决定写文件被审批门拦下 → 人授权 → 带 token 重新驱动完成 → 从事件流恢复）。

### Design Rationale

- **为什么业务逻辑只按 capability 选模型、vendor_ref 必须保留**：供应商差异（消息格式、流式事件、错误形状、价格与隐私属性）被收敛成稳定能力，换供应商不换业务逻辑；同时 `vendor_ref` 保留每次调用的原始响应引用，让审计能回到供应商真相，而内核本身不依赖任何供应商的形状——可替换与可审计是同一份契约的两面。
- **为什么真实供应商适配器是插件而非内核**：内核不依赖任何供应商，正是「能力结晶成插件」的赌注——网络接入、鉴权、供应商 SDK 都是易变的外层，只有契约是稳定的。`LocalEchoAdapter` 作为参考实现，让契约正确性与 Agent Loop 集成可以离线、确定性验证，不拖任何真实供应商进场。

### Notes & Caveats

- `LocalEchoAdapter` 是契约参考实现，不是真实模型；真实供应商适配器（OpenAI / Anthropic / 本地模型）待以插件接入。
- `estimate_cost` 暂无消费方。
- 并行 tool_call 暂不支持，响亮拒绝。
- 新增 14 个测试，总数到 213。

## [0.13.0] - 2026-09-29

### Features
- Router 保守落地（vision 原则一、runtime-arch §7，schema 不变，仍为 v6）：`RouteDecision`（continue / fork / rebirth / switch_recipe / spawn_subagent + 理由 + 触发信号 + 建议配方），决策记 `route.selected` 账本事件；Router 只读信号、不修改长期记忆。决策优先级：用户显式指令 > 挂起审批（继续等人）> 饱和度（fork / rebirth / continue）；歧义指令保守默认 continue。CLI：`route --session [--task-type] [--instruction]`。
- 信号层经审查加固：挂起审批按调用关联（`tool.requested` 的 `arguments_hash` 须匹配 `tool.approval_granted`，定向只读 SQL 投影，无截断窗口），不再按全局数量配对；饱和度锚定最近一次 `context.assembled`（handoff / rebirth 边界），只计边界后的 live context，rebirth 后自动重置；reason 用固定模板，不嵌入指令原文。
- README 整合：状态从「最小原型」更新为「本地优先的内核与运行时骨架已落地，无外部依赖」；新增「已落地的能力」表格（12 层能力 + CLI / Python 入口）与「尚未做（诚实边界）」（真实 Model Adapter、自然语言抽取 / 向量检索、多模态视觉、网络隔离 / 资源限额、暂停 resume）；验证 19 个命令真实存在。

### Design Rationale

- **为什么挂起审批必须按调用关联、饱和度必须锚定边界**：路由的信号若按全局数量配对、或把全部历史都计入饱和度，就会「自信地解释一个错误的世界」——重试风暴时明明仍有未答审批却被判为无，rebirth 之后饱和度永不重置、每次都触发 rebirth。路由的正确性完全建立在信号之上，信号层必须用定向只读投影把世界算准，路由的决策才可信；信号失真一寸，路由就错一丈。

### Notes & Caveats

- `switch_recipe` / `spawn_subagent` 是合法的决策输出，但执行者尚未接入——后续由 AgentLoop 与子 agent 机制承接。
- 饱和度阈值是启发式，可注入覆盖，不是内核常量。
- 新增 13 个测试，总数到 199。


## [0.12.0] - 2026-09-29

### Features

- 执行世界/沙箱落地（架构 §5 能力层最后一块，schema 不变，仍为 v6）：新增 `noname_harness/sandbox.py`（`Sandbox`）——文件读/写强制约束在工作区根目录内；命令执行为允许列表 + 硬超时 + 输出捕获；产出注册进 ToolRegistry 的工具（`read_file`=never、`write_file`=always、`run_command`=destructive/always），沙箱边界与审批门是两层独立防线；每次操作产出证据事件。
- 默认允许列表只含纯只读且无 exec/write hook 的命令（`ls`/`cat`/`echo`/`grep`/`wc`/`head`/`tail`/`pwd`/`date`）；`git`/`find`/`sed`/`awk`/`xargs`/解释器（`pytest`/`python3`）全部移除——它们本身就是任意执行原语（`git -c alias`、`find -exec`），执行面无法枚举。
- argv 路径扫描：任何解析为路径的命令参数必须在工作区内——只读命令带 `/etc/passwd` 也能 exfiltrate，必须堵。
- 写入经 `store.write_text_nofollow`：open 用未 resolve 的原始路径 + `O_NOFOLLOW`，TOCTOU 窗口关闭（resolve 只用于边界判定）；`O_NOFOLLOW` 缺失的平台 fail-closed。
- DB 侧车防护：名称（大小写不敏感，含 `-journal`）+ inode（`os.path.samefile` 对 db + 全部侧车）双重判定，hardlink 穿透被堵。
- 命令在独立进程组运行，超时 `killpg` 杀整组；pipe 排空无界改有界；非零退出记 `tool.failed`；`execve` 失败入账。
- 二进制文件以 hex 无损存储并标记 `encoding`；`read_file` 拒绝 FIFO（防永久阻塞）；截断有 `truncated` / `evidence_chars` 标记。

### Design Rationale

- **为什么允许列表按可执行名过滤不充分**：`git`/`find` 这类「安全」命令本身就是任意执行原语——`git -c alias.x=!cmd`、`find -exec`、`sed -e e` 都能借它们的合法外壳执行任意命令，执行面无法枚举。按名字过滤只是「尽量拦截」：每漏掉一个 flag 或子命令组合就是一个洞。默认只允许纯只读、无 exec/write hook 的命令，才让「越界执行不可能发生」在结构上成立，而不是靠拦截清单的完备性祈祷。
- **为什么写入必须用未 resolve 路径 + O_NOFOLLOW**：`resolve` 会静默跟随 symlink，校验时解析出的真实目标与写入时的真实目标之间隔着 TOCTOU 窗口——攻击者在窗口内替换 symlink 即可把写出到边界外，而所有检查都「通过」。resolve 只用于边界判定；open 时用未 resolve 的原始路径加 `O_NOFOLLOW` 拒绝最终组件 symlink，校验与写入才对最终组件原子，窗口在结构上被关闭而不是被缩小。

### Notes & Caveats

- 解释器 / test runner / `git` / `find` 等需显式 opt-in（`allowed_commands`），此时边界弱化为仅审批门——沙箱层不再提供物理保证。
- 沙箱不含网络隔离、资源限额（CPU/内存）、chroot/namespace，那属更深的沙箱层，后续按需叠加。
- 经两轮对抗性审查（两次 REJECT 后修复），共修复多个 REJECT 级漏洞，含一个真实毁库向量：hardlink 到 `harness.db-wal`。
- 新增 22 个测试，总数到 186。


## [0.11.0] - 2026-09-29

### Features

- 品味卡片 CLI 落地：新增 `card-propose` / `card-create` / `card-review` / `card-queue` / `card` 五个命令，卡片层与 taste 层一样可从命令行完整使用；`card-review` 的 split 通过 `--edited` JSON 传入子卡内容。
- 全局审查跨模块加固（schema 不变，仍为 v6）：一次通读全库的三视角审查，专找模块接缝处的内核削弱，逐项修复如下。
- `store.query()` / `query_one()` 强制只读（仅 SELECT / WITH / 只读 PRAGMA）：堵死「公共只读 SQL 入口可 INSERT 绕过审核门」的写后门。
- tombstone 与 generation 跨重启从 `tool.registered` 事件重建；generation 语义细化——shadow 与显式 unload 换代，进程重启的幂等重注册保持代不变。
- 失败执行的令牌在重建时按 oldest-first 重放：reserved 之后若 `tool.failed` 则释放回——修复「已消费令牌重启后复活」的安全 bug 与「瞬时失败吞授权」。
- Agent Loop 接受 `ToolRegistry`，tool_call 一律路由过审批门，不再信任 driver 内嵌的 result；无 registry 即契约违反；gated 工具需批准时停为 `waiting_approval`。
- 插件 `_rollback` 改用 `register` 的 `_restore` 路径恢复被遮蔽的原工具：恢复是 undo 而非重注册，不触发 tombstone。
- 卡片候选进入 `review_inbox`（`card_pending`）；`state_revisions` 补 `one_child_per_parent` 唯一索引；卡片 review 事件独立为 `taste.card.reviewed`；loop / tool / plugin / taste 的 bookkeeping 事件不再涌入 low 证据窗口。
- 导出补全（`PluginError` / `ModelProfile` / `ModelCapability` / `Recipe` / `resolve_recipe`）；`validate_output_path` 防止写出 `harness.db-wal` / `harness.db-shm` 等兄弟文件；CLI package 写文件委托 `store.write_context_package`（支持 rendered 文本）。

### Design Rationale

- **为什么公共 query 必须强制只读**：`store.query()` 是所有服务被告知使用的接缝。接缝若可写，任何一行 SQL 都能绕过 ToolRegistry 审批门直接 INSERT——「写入是提案不是事实」在 DB 层的最后一道防线就此失效。把公共查询入口在结构上限定为只读，让绕过审核的写入不再靠约定禁止、而是在物理上不可能。
- **为什么 Agent Loop 的工具调用必须路由过审批门**：审批门若只对「自愿使用 ToolRegistry 的代码」成立，那主 orchestrator 本身就是最大的旁路。loop 不再信 driver 自煮的结果，每个 tool_call 都走 `validate → approval → execute → log`——「审批是物理不可能」只有落在真正的执行路径上才算成立，否则只是文档里的愿望。

### Notes & Caveats

- 本轮修复包含一个真实安全 bug：已消费的审批令牌因重放顺序错误在重启后复活。
- 恢复仍是只读重建，不能 resume 暂停的 run——`waiting_approval` 之后需开启新 run。
- 真实 Model Adapter、执行世界沙箱、自然语言抽取、向量检索、多模态视觉仍未做。
- 新增 9 个测试，总数到 164。


## [0.10.0] - 2026-09-29

### Features

- 插件运行时保守契约落地（架构 §9，schema 不变，仍为 v6）：新增 `noname_harness/plugins.py`——`PluginManifest`（id / version / capabilities / max_permission / 接口兼容范围 / 迁移 / 是否请求全局作用域）、`Plugin`（manifest + build 工厂）、`PluginRuntime`（load / unload / loaded_plugins）。
- 内核不可谈判：插件贡献的工具必须走 ToolRegistry 审批门，无侧通道；manifest 先验证再加载；插件工具不得超过 manifest 声明的 `max_permission`；默认非全局作用域，未显式请求不得使用 global，不偷偷注册进程级全局状态。
- 可逆生命周期与原子性：卸载回收插件全部贡献；加载（注册 + `plugin.loaded` 账本记录）整体原子，失败回滚全部贡献并恢复被遮蔽的原工具（`register` 返回被取代工具对象，rollback 重新注册而非仅 unregister），记 `plugin.load_failed`；`build()` 记 `plugin.build_started` / `plugin.build_failed`，异常归一化为 `PluginError`。
- 工具实例代（generation）：每次注册换代，审批令牌绑定签发时的代；重注册 / 遮蔽 / unload 后旧令牌自动失效——令牌不比它授权的确切工具活得更久。
- 只做能力结晶的加载 / 校验 / 生命周期 / 审计接缝，不做插件市场。
- 修复品味卡片复核队列在秒级时间戳下的非确定排序：改用 rowid 决胜。

### Design Rationale

- **为什么令牌必须绑定工具实例代**：令牌若只绑定名字，unload 之后同名新实例就能用旧令牌执行——授权就比它授权的确切工具活得久。把令牌绑定到签发时的代，让授权严格等于「这一次、这个工具、这组参数」；工具实例一换，旧授权自然作废，无需额外的吊销逻辑，事件流里每一次批准都指向唯一确定的对象。
- **为什么失败回滚必须恢复原工具而非仅 unregister**：插件可以合法遮蔽宿主工具。若遮蔽完成后另一部分加载失败，仅 unregister 会把宿主原工具永久删除，并把 tombstone 顶到更高水位——宿主状态被一场失败的加载永久改写。原子加载 + 回滚时恢复原工具，让失败从不留下僵尸，也从不破坏宿主既有状态：加载要么整体成立，要么像没发生过。

### Notes & Caveats

- 不做插件市场，不做动态工作流合成；插件只是被验证过的能力结晶的加载接缝。
- `build()` 是任意代码：宿主不得向插件传递 registry / store 句柄，插件的一切贡献必须经 manifest 声明与 ToolRegistry 审批门。
- 新增 13 个插件测试，总数到 153。


## [0.9.0] - 2026-09-29

### Features

- 品味卡片层落地（schema 到 v6）：新增 `noname_harness/taste_cards.py`（`TasteCardService`）与新表 `taste_cards`——append-only、版本化、`supersedes` 链、INSERT 边界防护、`one_child_per_parent` 唯一索引。定位与 docs/taste-cards.md 一致：卡片是品味证据的视图与复核体验（「还是我吗」），不是新事实格式，绝不进入上下文包事实层。
- 确定性聚类替身：`propose_clusters` 按 `(scope, track)` 分组已审核活跃品味，可解释、无意外合并；未来语义/embedding 聚类器可替换，但不绕过同一存储/审核契约。
- 卡片内容完整：标题 / 一句态度 / `track`（authored | adopted | mixed）/ `scope` / `taste_ids` / 代表证据 / 张力 / 影响范围 / 状态 / 图像元数据。
- 生命周期状态机：`candidate→{accept,edit,retire,split}`、`active→{edit,pause,retire,split}`、`paused→{edit,resume,retire,split}`、`retired` 终态；仅 head 可审；review 在事务内写锁下重检 head 防并发分叉；edit 重跑创建时的校验。
- split 原子：全部校验前置，退休原卡 + 创建互斥候选子卡在单一事务内完成（要么全写要么全不写）；子卡继承图像契约；校验完备且互不相交。
- 图像契约：image 仅为视觉隐喻，记录 `model / prompt / seed / version` 保证可重建；默认纯排版（`None`）；绝不用图像反推品味；本层不做真实生成。
- 复核队列确定性：候选优先（`recorded_at` 最久优先、`id` 决胜），active 按最久未确认排序；无随机稀有度、无赌博机制。
- stale 标注：卡片分组 taste 不再全是活跃 head 时，`_project` 标注 stale，提示复核而不阻断、不自动修改。

### Design Rationale

- **为什么卡片是视图而非新事实**：品味卡片帮助你复核，不替你定义永远正确的画像。它只做分组与提示——事实仍在 taste 层，卡片对同一证据可以合并、拆分、退休而事实不动；若卡片本身成为事实格式，复核层的编辑就会反向污染证据层，溯源随之分叉。所以卡片绝不进入上下文包事实层，它只是「这还是我吗」的提问界面。
- **为什么 split 必须原子且图像只记契约**：split 非原子会让账本永久不一致——原卡已退休、子卡残缺，事件流里出现悬空的 supersedes 链；所以全部校验前置、退休与创建在单一事务内完成。图像只是视觉隐喻：记录 `model / prompt / seed / version` 已足以保证可重建，但绝不用图像反推品味，默认纯排版——最准确画出用户的卡不是目标，最容易帮助用户思考的卡才是。

### Notes & Caveats

- 聚类是确定性替身：`(scope, track)` 分组，无语义/embedding；替换聚类器不得绕过同一存储与审核契约。
- 本层不做真实图像生成，只记录可重建元数据契约。
- stale 仅标注提示复核，不自动处理、不自动修改卡片。
- 新增 16 个测试，总数到 139。


## [0.8.0] - 2026-09-29

### Features

- Agent Loop 显式状态机（schema 不变，仍为 v5）：新增 `noname_harness/agent_loop.py`，状态序列 `IDLE → ASSEMBLING_CONTEXT → SELECTING_MODEL → CALLING_MODEL → WAITING_TOOL → APPLYING_RESULT → CHECKING_STOP → COMPLETED / FAILED / CANCELLED`，含合法转移表，非法转移即 `AgentLoopError`；`STREAMING_OUTPUT` / `COMPACTING` 因原型层无驱动可达已删，状态表与代码严格一致。
- loop 只驱动：不路由模型、不写记忆、不判权限——上下文组装复用 `assemble_context_package`、配方选择复用 `resolve_recipe`、工具调用经 `ToolRegistry`；`CALLING_MODEL` 委托给注入的 `SessionDriver` 协议，原型用确定性 stub，生产接 Model Adapter。
- 转移全入账：每次状态变化都是 `loop.transition` 事件；停止条件明确（任务完成 / 用户暂停 / 轮次上限 / 预算上限 / 不可恢复错误 / 等待批准）。
- 失败归一化与可恢复：驱动异常与契约违反（矛盾 `LoopResult`、未知 `stop_reason`）统一归一为 `FAILED`，经 `_force_fail` 写真实 transition（`forced: true`）到终态；`reconstruct()` 从事件流重建状态 / 轮次 / limits（从 `loop.started` 读回），不靠进程内对象，致命失败可恢复、不留僵尸。
- `LoopResult` 边界校验：`tool_call` 不得与 `task_complete` / `stop_reason` 共存，`task_complete` 不得与非 `task_complete` 的 `stop_reason` 共存；`max_rounds` / `budget_rounds` ≥ 1。

### Design Rationale

- **为什么 loop 只驱动，不路由、不写记忆、不判权限**：把路由、记忆、权限全塞进一个巨大驱动函数是 agent loop 的病——行为藏在隐式控制流里，无法回放、无法单测、职责无归属。显式状态机让每一步推进都是一个可入账的转移，上下文组装、配方选择、工具执行各自复用已有服务，每个职责有单一归属；loop 退化为薄而可审计的驱动层，正符合「内核薄而不可谈判」。
- **为什么致命失败也要写真实 transition 而非只改进程内状态**：恢复从事件流重建，不靠进程内对象。若异常路径只把内存里的状态标成 FAILED 而不落事件，重建会从最后一个正常事件把死运行复活成中途状态——一个看似可继续的僵尸。`_force_fail` 写下的 `forced: true` transition 让恢复看到终态、让审计看到「机器是被中止的，而非被正常驱动到 FAILED」，事件流在失败路径上依然是事实来源。

### Notes & Caveats

- 本层使用确定性 stub driver，不含真实模型调用、流式输出与压缩逻辑；`STREAMING_OUTPUT` / `COMPACTING` 待真实驱动可达时再恢复。
- 新增 17 个测试，总数到 123。

## [0.7.0] - 2026-09-29

### Features

- 工具注册表内核（schema 不变，仍为 v5）：`noname_harness/tools.py` 将 `ToolSchema`（模型可见面：name / description / input_schema）与 `Tool`（宿主执行面：callable / permission / approval / scope / session_id）严格分离，`visible_tools` 只向模型暴露契约。执行管线为 `validate → approval → execute → log → return`。
- 审批是账本支撑的一次性令牌而非布尔：`grant_approval`（授权方路径）铸造令牌并记 `tool.approval_granted`，绑定（工具名, arguments 的 sha256, approver, session_id）、单次使用；执行时验证并消费，记 `tool.approved` 引用既有授权。未消费令牌可从账本重建（跨重启持久），已消费令牌跨重启仍拒绝。执行器不给自己打分。
- 结构性护栏：`register` 拒绝非精确 `Tool` 实例（防子类化覆写审批门）；遮蔽单调性（不得降低 permission、不得丢弃 approval、宽作用域不得遮蔽窄作用域）；tombstone 记录每个名字史上最强门，`unregister → re-register` 也不能降级；destructive 工具必须 `approval=always`。
- 作用域真实生效：session 工具绑定 `session_id`，其它 session 不可见、不可调；删去了无强制力的 agent 作用域，诚实保留 global / session 两级。
- 账本完整与最小披露：审批前只存 arguments 哈希，不把模型可控内容落盘；validation 失败、执行失败都入账；记录真实 `elapsed_ms`；执行失败把令牌放回 live set——授权是 per-completed-call 而非 per-attempt，瞬时错误不吞授权。

### Design Rationale

- **为什么审批必须是账本支撑的令牌而非布尔**：自证布尔意味着任何能调用 request 的代码都能授权，执行器还自己写 approved——账本记录的是调用方的断言而非事实，可以被伪造。令牌把授权变成可验证的持久证据：绑定到确切的参数哈希与 session、单次使用、可从账本重建，审批不再依赖「调用方说自己被批准了」，而是账本上确实存在过一笔由授权方写下的 `tool.approval_granted`。
- **为什么要单调性 + tombstone**：审批门不只是「调用时拦截」，还要防「注册时降级」。如果同名注册或 unregister 后重注册可以削弱门，攻击面就从「能不能绕过审批」变成「能不能抢先注册一个同名弱门工具」——物理门退化为命名竞争。单调性与 tombstone 让「一个名字曾达到的门强度只升不降」成为结构事实，降级在注册层就被拒绝。

### Notes & Caveats

- 本层只执行宿主显式注册的 callable，不含真实 shell / 网络 / 文件副作用——执行世界（沙箱、超时、并发）属下一阶段。
- agent 作用域被删除，因为原型层没有对它的强制力；保留它只会制造「有作用域」的假象。
- 新增 27 个工具测试，总数到 106。


## [0.6.0] - 2026-09-29

### Features

- 模型能力契约（schema 不变，仍为 v5）：`noname_harness/models.py` 新增 `ModelCapability`（reasoning / vision / tool_calling / context_window）与 `ModelProfile`（目标模型能力 + 预算姿态 low/medium/high）。纯契约层，不调用任何真实外部模型。
- 配方注册表：`noname_harness/recipes.py` 新增 6 个默认配方（question / research / code-change / memory-write / taste-card / high-risk），把任务类型映射到建议的角色链，对齐 docs/runtime-architecture.md；配方是建议而非锁死，注册表可注入覆盖；memory-write 配方强制 extractor 与 conflict-checker 由不同角色承担。
- 按模型投影接续包：`assemble_context_package` 接受可选 `model` 与 `task_type`。核心不变式是「事实对任何模型一致」——已审核法典、任务态、品味、provenance 完全不变，只按目标模型的预算姿态与 context window 收紧低层证据窗口（low 约 1/4、high 约 2 倍、<16k 窗口再减半、地板 3）；投影目标与配方记入 assembly 元数据与 `context.assembled` 账本事件。
- CLI：`package` 新增 `--model-id` / `--budget` / `--context-window` / `--task-type`；新增 `recipes` 命令查看默认配方。

### Design Rationale

- **为什么投影只裁剪低层证据窗口，而不动法典 / 任务态 / 品味 / 溯源**：上下文包是投影，不是数据库——换模型换的是投影参数，不是事实来源。低层是「最近工作现场」，本来就是最易从事件流重建的一层，裁剪发生在这一层代价最小、可逆性最强；而已审核的法典、任务态、品味是经过人工确认的稳定层，若因目标模型不同而改变，等于让「给哪个模型看」反过来决定「事实是什么」，溯源也会随之分叉。所以稳定层对任何模型逐字一致，只有低层窗口随预算伸缩。
- **为什么配方是「建议 + 入账」而非直接路由**：原型没有真实外部模型可路由，此时做自动路由只是空转。但把每次推荐的配方、角色链与理由记入 assembly 元数据和 `context.assembled` 账本事件，等于提前积累了路由决策的证据——未来接入真实路由时，规则不是从空白开始设计，而是可以回放账本、回答「这次推荐是否合适」，路由因此可解释、可审计。

### Notes & Caveats

- 本步仍是契约层：不含真实模型调用、流式输出、计费、向量检索；`ModelProfile` 只是投影参数，不验证目标模型是否真实存在。
- 低层裁剪有地板（窗口至少保留 3 条事件），保证即使最低预算下接续包仍可接管。
- 新增 6 个测试（契约、配方、按预算与窗口投影、元数据入账、CLI 参数），总数到 75。

## [0.5.0] - 2026-09-29

### Features

- 第二轮对抗性审查 + 加固（schema v5）：在不改变公开契约的前提下，把「结构上不可能」再向存储层推进一步。
- supersede 链加 INSERT 边界触发器：拒绝自引用与前向引用，cycle 在结构上不可能发生；外键从声明变为真实生效。
- 提案评审与品味评审都在写事务内串行化：并发 review 不再静默互相覆盖；`accept → retire` 生命周期保持不变。
- 品味渲染进接续包时使用围栏块，task 字段单行化：品味与自由文本无法在渲染层冒充事实结构。
- 品味审计轨迹沿 supersede 链跨版本聚合；断链 / cycle 检测由静默截断改为大声报错。
- adopted 品味的来源事件并入包 provenance；`record_authored` / `propose_adopted` 拒绝 falsy 内容；`retire` 继承前驱 `valid_from` 并闭合 `valid_to`；CLI 拒绝 falsy `--content` 与无效 `--valid-from` / `--valid-to` 组合。

### Design Rationale

- **为什么断链要报错而非截断**：审计轨迹静默消失是「证据不可丢」最坏的失败方式——投影照常生成，只是证据悄悄不见了，没有人会发现。宁可拒绝投影、让问题暴露在明处，也不能让溯源链在无人察觉的情况下缺一环。
- **为什么评审要在事务内重检**：pending 是派生属性而非存储字段，事务外的预检无法阻止并发双写——两个评审可以同时通过检查再各自提交。`BEGIN IMMEDIATE` 写锁下的重读让后到的评审看到 head 已移动而中止，而不是分叉出第二个 head 或覆盖前者。

### Notes & Caveats

- 新增 26 个加固 / 暴力 / 迁移保真 / 回归测试，总数到 69；暴力测试覆盖 SQL 注入、路径穿越、FTS 特殊字符、超大证据、falsy 输入，以及 40 步随机工作负载下的不变量。
- 迁移保真测试的「v3 旧库」是用当前代码建库再回退版本号合成的，并非真实历史 v3 二进制产物——这是一个已知的、有意记录的近似。
- 品味卡片 / 聚类 / 视觉仍未做；品味层为纯文字双轨。

## [0.4.1] - 2026-09-29

### Features

- 对抗性审查修复轮（skeptic / architect / minimalist 三视角）：在不改变公开契约的前提下收紧内核。
- 品味生命周期改为显式状态机：`candidate → {adopt, retire}`、`active → {edit, pause, retire}`、`paused → {edit, resume, retire}`、`retired → {}`（终态）。
- 品味 review 仅作用于 lineage head，并由 `taste_records(supersedes_id)` 唯一索引在结构上杜绝分叉。
- bitemporal 从「只写不读」变为真正生效：`active_state` / 接续包投影按 `as_of` 过滤有效期，过期与未生效的事实自动退出；`retire` 默认以审核时刻闭合 `valid_to`；`valid_from` / `valid_to` 经严格 ISO-8601 解析并归一化到 UTC。
- store 暴露公开服务 API（`transaction` / `record_event` / `query` / `query_one` / `check_event_ids`），TasteService 不再访问任何私有成员。

### Design Rationale

- **为什么状态机必须显式**：原转移表把 `edit` / `resume` 都映射到 `active`，使一条 adopted 候选可以绕过强制 `adopt` 直接激活——这正是「adopted 必须显式审核」要防的事。把转移写成表格后，「edit 不能激活候选」「retire 不可逆」成为结构事实而非约定。
- **为什么 bitemporal 必须有读侧语义**：只记录 `valid_from` / `valid_to` 而不在投影中使用，会让一条昨天就过期的约束仍被当作当前法典交给新会话。有效期只有在投影层真正过滤时才成立；supersede 链与冲突检测则用 `apply_validity=False` 看到全量历史，保证链不因有效期而断裂。

### Notes & Caveats

- 新增 10 个对抗性回归测试，逐一钉死每个已修复漏洞，含「关闭并重开数据库后凭接续包接管」的真实新会话场景。
- 品味卡片 / 聚类 / 视觉仍未做；品味层为纯文字双轨。

## [0.4.0] - 2026-09-29

### Features

- 双时序（schema v4）：`state_revisions` 新增 `valid_from` / `valid_to`，把「事实在世界上何时为真」（valid time）与「系统何时知道」（recorded time）分成两个独立维度；`recorded_at` 仍由系统自动写入，valid 边界由审核人在 `review` 时通过 `--valid-from` / `--valid-to` 显式声明。
- `retire` 即版本化失效：让一条状态失效是写入新的 revision 并可附带 `--valid-to` 记录它在世界上何时停止为真，而不是删除历史。
- 审核收件箱：新增 `store.review_inbox()` 投影与 CLI `inbox` 命令，把待审法典候选（高层）、任务态候选（中层）和品味候选收拢成一张待办清单，每条附影响范围、来源事件、冲突引用和提出理由。

### Design Rationale

- **为什么区分 valid 与 recorded**：一条状态「何时成立」和「系统何时得知」经常不同步——一个约束可能上周就生效，今天才被记录；一个任务态可能昨天就已过时，今天才被失效。只记录 recorded time 会把这两个问题混为一谈，导致回放历史时无法回答「当时的世界是什么样」。valid time 是人对事实的判断，所以必须由审核人显式声明，系统不猜测。
- **为什么收件箱是投影而非存储**：待审事项的全部信息（候选、来源、冲突、理由）已经存在于 append-only 表中；再存一份收件箱状态只会引入双写和一致性问题。收件箱完全由投影派生，可随时重建、可替换实现，自身不成为事实来源。这也让 docs/ledger.md 的「审核收件箱」从设计变成了同一份基座上的真实命令。

### Notes & Caveats

- schema 已到 v4；迁移链 v1→v2→v3→v4 每步幂等，每步有对应测试，旧库打开时自动逐级迁移。
- `valid_from` / `valid_to` 是可选项：未声明时只表示「valid time 未知」，不影响既有行为；`valid_to` 早于 `valid_from` 会被存储层拒绝。
- 收件箱是只读聚合视图，批量处理策略（低风险同质候选才可批量）尚未实现，当前全部逐条审核。

## [0.3.0] - 2026-09-29

### Features

- 品味双轨层（schema v3）：新增 `noname_harness/taste.py`（TasteService）与 `taste_records` / `taste_reviews` 两张 append-only、版本化、带 supersedes 链的表。
- 两条来源轨道：Authored（自述）写下即确认、立即激活，权威最高；Adopted（采纳）必须引用至少一个来源事件，始终以 `candidate` 进入，经显式 `adopt` 审核才激活。
- 品味生命周期：`adopt / edit / pause / resume / retire`，每次审核写入不可变的审核记录，`edit` 产生取代旧版本的新记录。
- 上下文包新增独立 `preference` section：标注 `influence: soft` 及约束说明，provenance 单独记录 `taste_ids`；品味不进入高、中、低任何事实层。
- CLI 新增 `taste-add` / `taste-propose` / `taste-review` / `taste` 四个命令。

### Design Rationale

- **为什么品味必须独立成区**：品味影响的是态度——方案排序、表达风格、取舍偏好——而不是事实。把它混入事实层会让「我喜欢什么」和「什么是真的」在下游模型眼里变得不可区分，溯源也会断。独立 section 加显式 soft influence 标注，让新会话能正确加权：可以参考，但绝不当证据，也绝不降低验证标准。
- **为什么 adopted 必须显式审核**：模型表现出的倾向只是观察，不是用户的立场。未经确认的倾向永远停留在 `candidate`，不能进入活跃品味层；只有用户显式 `adopt` 后它才生效，且永远标记为 `adopted`，不能伪装成用户自述。这条门槛保证品味层的每一条活跃记录都有明确的责任人。
- **为什么品味层复用 append-only 与版本化**：纠正品味和纠正事实一样，应该是「写入新版本取代旧版本」，而不是抹掉历史。supersedes 链让品味的演进本身成为可回看的证据——这正是品味卡和未来复核体验的数据基础。

### Notes & Caveats

- 品味卡片、聚类、视觉生成仍未做：当前是纯文字记录的双轨 MVP，docs/taste-cards.md 描述的卡片与复核体验依赖后续的聚类与图像能力。
- schema 迁移 v2→v3 为新增两张表，幂等且可测；不触及既有事件、证据与状态表。
- authored 品味的来源事件为可选（用户本人即来源），但附上来历事件可让溯源更完整；adopted 的来源事件为强制。
