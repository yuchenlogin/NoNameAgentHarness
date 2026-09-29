# Changelog

> 只记录版本级变化：新功能、重大重构、架构调整、破坏性变更。不是每个 commit 都有条目。

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
