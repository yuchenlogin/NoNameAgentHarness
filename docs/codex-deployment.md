# 在 Codex 中使用 NoName

## 先完成一次接续

安装并启用项目后，正常讨论一个决定，调用 `$noname-remember` 提出候选，再用
`$noname-ledger` 打开本地网页确认。新聊天中调用 `$noname-recall`，检查已批准决定、
理由及来源。这条“提出 → 确认 → 接续”的路径就是最小成功体验。

日常工作分三层：hooks 自动留证与提供启动上下文；agent 在重要阶段选择少量值得保留的
决定或进度；你确认长期记忆与品味。保留一段证据不会自动把它变成项目规则。

## 接入范围

NoName 以本地 **memory sidecar** 接入 Codex：记录输入/最终答复、组装上下文包、
保留候选与审核、检索证据、显示账本。Codex 仍负责模型调用、工具执行、权限与会话调度。
NoName 的 AgentLoop、Sandbox、ToolRegistry 审批令牌与 Router 不接管 Codex。
这不是替换 Codex runtime，也不需要另配模型 API key。

全局安装命令；每个项目单独启用数据库。只有存在已初始化的 `.noname/harness.db`
的项目才会被 hooks 记录。用户作用域的 taste 当前仍保存在该项目账本内，**不会自动跨项目共享**。

## 安装

需要 Python 3.10+、Git（可选，用于判断项目根）、支持 skills/hooks 的当前 Codex。
生命周期桥接只使用 Python 标准库，无常驻后台模型服务、无外部模型调用。
打开可审核账本时才启动本机 HTTP 服务；无需审核可使用只读离线导出。

```sh
git clone https://github.com/yuchenlogin/NoNameAgentHarness.git
cd NoNameAgentHarness

# 有 Conda 时先激活自己的环境；否则用独立 venv。
python3 -m venv ~/.codex/noname/venv
~/.codex/noname/venv/bin/python -m noname_harness.codex_install
```

安装会创建：

| 路径 | 用途 |
|---|---|
| `~/.codex/bin/noname-codex` | 固定 Python 和本地源码路径的确定性命令入口 |
| `~/.agents/skills/noname-*` | 全局 skill symlink，指向本仓库的 `integrations/codex/skills/` |
| `~/.codex/prompts/noname-*.md` | CLI/IDE 的 deprecated slash 兼容入口 |
| `~/.codex/hooks.json` | SessionStart、UserPromptSubmit、Stop handlers |

安装器不改 `AGENTS.md`、模型/供应商配置、shell 配置。已有 hooks 合并保留，首次修改前
备份为 `hooks.json.before-noname`。同名用户文件或 skill 会阻止安装，不会被覆盖。
重复安装是幂等操作。源码 symlink 是本地开发部署，仓库必须保留在原路径；移动仓库前先卸载再安装。

安装器协调自身的并发进程，并在原子替换前检测文件是否被修改；不协调编辑器或其他
安装器的最后比较/替换窗口，因此安装、卸载期间不要并发编辑同一个 hooks 文件。
卸载先移除 hook 注册，再移除入口；注册移除失败时保留可运行依赖，便于重试。

`CODEX_HOME` 可覆盖 Codex 配置目录；skill discovery 默认仍使用 `~/.agents/skills`。
自定义配置目录时，安装及调用时都设置同一个 `CODEX_HOME`，不要另用第二套路径选择。
高级用途可使用安装器的 `--skills-home`、`--python` 参数。

## 信任 hooks 与启用项目

1. 重开 Codex CLI，执行 `/hooks`。检查三条 `noname-codex hook` 定义并信任。
   安装不会伪造 trust 状态，也不使用 `--dangerously-bypass-hook-trust`。
2. 在要启用的项目目录执行：

   ```sh
   ~/.codex/bin/noname-codex init
   ~/.codex/bin/noname-codex status
   ```

   或在 Codex 中显式调用 `$noname-init`。
3. 重开或 resume 该项目聊天，让 SessionStart 运行；输入一个普通问题并完成一轮答复。
4. 执行 `$noname-status` 检查当前 session 的 hooks 回执与 `integrity.ok`。

Git 项目默认用 checkout 根；子目录有独立账本时优先使用最近的账本。非 Git 项目使用 CWD，
不会向上猜测其他项目。想使用指定项目：`noname-codex --root /absolute/project status`。
复制数据库到另一项目会因 workspace_root 不匹配而拒绝；跨项目 symlink 数据库也拒绝。

hooks 遵循 Codex 自身的项目信任、运行权限与 timeout。若 hook 报权限错误，不代表 NoName
已经记录成功；检查 `/hooks` 日志并按当前运行环境授予对本地账本的写权限。
CLI 信任完成后仍需在实际使用的桌面聊天验证回执，不把 CLI 的成功当成桌面端成功。

## 显式命令

桌面端使用可选择的 skills，输入 `$noname-…`（部分界面使用 `@` 选择 skill）。CLI 使用
`/skills` 选择或 `$` 提及。**没有注册裸 `/noname-status` 自定义内建 slash 命令。**
CLI/IDE 兼容旧 custom prompts 的具体写法如下；这些 prompts 已被官方弃用，桌面端不保证支持。

| 功能 | Skill | CLI/IDE slash 兼容入口 |
|---|---|---|
| 启用当前项目 | `$noname-init` | `/prompts:noname-init` |
| 验证接入与完整性 | `$noname-status` | `/prompts:noname-status` |
| 读取上下文包 | `$noname-recall` | `/prompts:noname-recall` |
| 保存候选/进度 | `$noname-remember` | `/prompts:noname-remember` |
| 查看待审项 | `$noname-inbox` | `/prompts:noname-inbox` |
| 明确审核一个状态候选 | `$noname-review <id> accept` | `/prompts:noname-review <id> accept` |
| 检索账本 | `$noname-search 账本` | `/prompts:noname-search 账本` |
| 打开交互式账本 | `$noname-ledger` | `/prompts:noname-ledger` |

这些 UI 入口加载固定工作流，再由 Codex 调用脚本。需要完全确定的执行时直接用 CLI：

```sh
~/.codex/bin/noname-codex status
~/.codex/bin/noname-codex recall --task '继续当前项目'
~/.codex/bin/noname-codex search '账本'
~/.codex/bin/noname-codex remember --session manual-example --layer high --key storage --text '首版使用 SQLite' --reason '项目只在本地运行'
~/.codex/bin/noname-codex inbox
~/.codex/bin/noname-codex review --proposal-id prp_REAL_ID --action accept --reviewer user
~/.codex/bin/noname-codex ledger
~/.codex/bin/noname-codex verify
```

`remember` 记录摘要事件并提出候选。新候选返回 `status: proposed`、`event_id`、
`proposal.id` 与 `active: false`；相同的最新待审内容返回 `already_pending`，已生效且
无其他差异待审的同内容返回 `unchanged`，避免反复制造相同候选。`--reason` 保存决定理由，
`--next-step` 保存接续指针；长内容可用 `--text-file <UTF-8 文件>` 或 `--text-file -` 从 stdin 读取。
返回的 `change` 展示当前内容、候选内容和确认后是否替换旧版。

`high` 用于项目决定与约束，`mid` 用于当前阶段进度。为同一主题复用原有的 `layer` 和
`key`（例如 `storage`），不要为修正另造 `storage-v2`。确认后同键新版取代旧当前状态，
历史保留；不同内容的旧待审候选不会自动拒绝，需要在收件箱中明确处理。

`review accept` 才产生 active revision。`review` 是可信本地接口，`reviewer` 是审计标签，
并非用户身份认证；skill 不能把“保存”或历史模型意见当作审核授权。

`ledger` 返回本地审批页面的 `url`，可在浏览器中查看来源/冲突、填写签署人并直接审核。
审批会写入现有审核与版本记录，成功后刷新页面；旧页面提交已变化的候选会返回冲突。
服务默认一小时无活动后退出。只需离线快照时使用 `ledger --static`。

更完整的 taste、card、snapshot 等命令仍可使用项目现有 CLI：
`<venv-python> -m noname_harness <command> --db <project>/.noname/harness.db`（从仓库目录运行）。

## 自动生命周期与真实边界

- **SessionStart**：为已启用项目组装上下文，输出给模型；完整输出最多 12KB UTF-8，优先
  放入已批准状态，再考虑偏好、候选与原始证据。超预算的完整条目不注入，明确报告遗漏
  数量和完整 recall 入口。handler 使用 `additionalContextLimit: 0`，由桥接的严格字节上限
  控制预算，避免 Codex 再次按不同 token 估计裁剪。记录 context package ID 和 hook 回执。
- **UserPromptSubmit**：记录原始用户输入与 turn ID；重复交付同一 turn 不重复写入。
- **Stop**：记录 hook 提供的最后 assistant message；缺失时只记明确的空捕获回执。
  不阻止结束、不触发额外续跑、不自动批准候选。

捕获失败只输出 `systemMessage` 警告，不返回具有阻断/续跑含义的 exit 2。
失败时不留下成功捕获回执。`captured_text: false` 明确表示 hook 运行了但没有取得正文，
`context_prepared: true` 表示启动上下文已准备好，不代表模型已经采用它。

不会自动导入旧聊天，不抓取 Codex 内部数据库或不稳定的 transcript 文件，不记录工具输出、
中间 commentary 或所有 assistant messages。因此它是 **输入/最终答复级捕获，不是 Codex 全事件无损存档**。
要保留测试输出/文件 diff，应显式使用 NoName event/snapshot 等原有入口。
原始对话属于低层证据，只有审核后的条目进入长期状态；未审核的候选在上下文中明确标注。

启动上下文和 skills 会引导 agent 在决定变化、明确修正或阶段完成时进行少量策展。
这是一份工作流约定，不能当成后台自动语义抽取服务已经实现的证明。不要每轮提出候选，
也不要把所有测试成功或临时想法写入长期记忆。优先保留“决定 + 理由 + 来源”，工作进度
只保留继续任务所需的信息。模型推断的审美偏好始终是候选，必须经用户明确采纳。

`status` 区分 enabled、事件数、已批准状态、待审数量、近期 hooks 回执和当前 session 回执。
它不推测 hook trust。回执证明 hook 程序到达了相应阶段，**不证明模型之后确实遵循记忆**。
未提供当前 session ID 时，不能由其他聊天的回执断言本聊天已经接入。
不要用“在当前文件夹搜索不到 noname”代替接入状态检查。

## 常见情况

| 看到的情况 | 如何理解和处理 |
|---|---|
| `enabled: false` | 当前解析到的项目尚未启用；先确认 `workspace_root`，再运行 `init` |
| 事件很多，但已批准记忆是 0 | 自动留证已经发生；尚未确认长期内容。挑一件重要决定提出并审核即可 |
| 本聊天回执为空，近期其他聊天有回执 | 只能证明其他聊天运行过。恢复本聊天并完成一轮工作，再检查对应 session |
| 候选出现了，但新会话没有当作决定 | 候选未确认时不能当作事实；先检查收件箱与审核结果 |
| 修正后档案里仍看到旧话 | 历史保留是预期行为；当前有效决定以已批准状态为准 |
| 本地网页链接失效 | 服务可能已空闲退出；重新调用 `$noname-ledger` 获得新链接 |

页面阅读、签署操作与远程端口转发见 [账本指南](ledger.md)，选择什么值得记住见
[体验模型](experience.md)。

## 卸载

从本仓库使用原来安装的 Python 路径执行：

```sh
~/.codex/noname/venv/bin/python -m noname_harness.codex_install --uninstall
```

仅移除本接入的 launcher、skill symlink、slash prompts 与 hooks handlers，保留其他配置、
虚拟环境、备份与所有项目 `.noname/` 数据。若生成文件被人工修改，卸载会拒绝，要求先保留修改。

官方依据（2026-10-14 核对）：[Skills](https://learn.chatgpt.com/docs/build-skills)、
[Hooks 与 trust](https://learn.chatgpt.com/docs/hooks)、
[Custom prompts 的兼容机制与弃用说明](https://learn.chatgpt.com/docs/custom-prompts)。
