# 用户体验改进验收

完成日期：2026-10-16。本记录覆盖源码实现与配套插件的集成验收；GitHub 提交与 npm 发布分别管理。

## 实现边界

从首次提交 `6a07c77` 出发，按“记忆存决定、理由与接续指针，档案留细节”的原则重做阅读路径。
三位 sub agents 分别实际走查首次上手、Codex 长期协作、DSH 插件流水线；发现与取舍见 [体验记录](experience.md)。

本轮修改集中于账本展示、Codex/DSH 集成、测试和文档。开始时仓库已有未提交的内核及审核功能改动，这些均保留。
对比开始时的 SHA-256，`store.py`、`taste.py`、`taste_cards.py` 和 `context.py` 完全一致；本轮没有修改 schema、审核状态机或上下文包内核。

## 验证证据

| 验证 | 结果 |
|---|---|
| 主仓 Python 全量回归 | 783 passed |
| DSH TypeScript 构建与插件回归 | 构建通过；66 passed |
| 真正的 npm 分发边界 | npm pack → 全新 consumer 安装 → 从安装包导入，实际 Python/SQLite/HTTP 验证通过 |
| 共享展示与服务 | 5 个文件在主仓、vendor、全新安装包中 SHA 一致 |
| 实际 Chrome | 导航、页面/历史搜索、活动筛选、文本编辑、草稿恢复、签署修正、版本历史、同题候选对照、并发冲突、旧草稿显式核对通过 |
| 持续变化 | 只读更新能发现新证据和审核；生效/过期时间跨越无写入时也改变版本指纹；不打断草稿、不因轮询延长空闲期限 |
| 阅读与可访问性 | 桌面及 390px 窄屏无横向溢出，键盘可达，无浏览器 JS 错误；真实界面截图已检查 |
| 离线与渐进增强 | 静态快照无后端请求、无签署凭证；搜索和导航可用；禁用 JS 时全部页面内容仍可展开浏览 |
| Codex 实流水线 | 正常对话不产生长期候选；重复保存复用候选；同题修正确认后，新 SessionStart 读取新版，旧版留在历史 |

签署测试均运行于临时库并标明模拟身份，没有批准真实项目的候选。环境没有 Conda，使用已有 `.venv`；没有 `dsh` 可执行命令，因此没有宣称新一轮真实 DSH 宿主 UI 验收。插件边界使用实际 `defineTool`、Python sidecar、SQLite、HTTP 与独立安装包验证。

## 对抗性审查

依照 `adversarial-review` 技能实际运行三个 `codex exec --sandbox read-only` CLI reviewer：Skeptic、Architect、Minimalist，并完成一次聚焦修复的 CLI 复查。没有用角色走查代替代码审查。

| 审查发现 | 处理与验证 |
|---|---|
| 历史页搜索只返回零项 | 版本/来源条目建立可搜索索引，搜索展开可读版本；真实 Chrome 回归 |
| 同题多个待审候选像无关内容 | 明示先前/较新提议并可对照；不自动拒绝或采纳任何候选 |
| 草稿自动保存重置了过期版本标记 | 保留原比较令牌；必须明确核对继续或丢弃；修改理由后再次刷新仍保留警告 |
| 退役/过期 head 被叫作当前版本 | 与现有 active-state 投影对照，标为历史版本；未来、过期、退役均有回归 |
| `autoIngest=false` 时描述仍称自动捕获 | 工具描述随配置变化，两种配置均有回归 |
| QA 修改造成 DSH 临时展示漂移 | 最终重新同步，源/vendor/打包安装版比对 SHA |
| 时间跨过有效期而没有数据库写入 | 指纹加入已跨过的生效/失效边界；用冻结时间与真实 HTTP 验证激活、包含结束瞬间、到期后失效 |
| GitHub macOS 上服务在监听前启动超时 | 数值 loopback 绑定跳过标准 HTTPServer 的反向 DNS 查询；真实服务在 DNS 不可用时仍可启动并响应健康检查 |

所有已确认问题均已修复，无未解决的高、中严重度发现。安装的 skill 缺少 `brain/principles.md` 及其关联原则文件；审查使用实际存在的 lens 定义与明确原则名，不声称已读取缺失文件。

GitHub 提交后的 macOS 启动问题另经 Skeptic CLI 复查，结论为 PASS、无可操作发现。数值 loopback 的反向 DNS 阻塞也与 [GitHub runner-images #14409](https://github.com/actions/runner-images/issues/14409) 的维护者诊断一致；本项目保留原启动期限，修复无关的网络依赖。

## 重现

在仓库根目录，用自己的 Python 开发环境运行：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m examples.ledger_demo > /tmp/noname-ledger-demo.json
```

可选真实浏览器验收，需要本机 Chrome 与开发用 Playwright；不增加产品运行依赖：

```sh
npm install --prefix /tmp/noname-ledger-browser-qa playwright
NODE_PATH=/tmp/noname-ledger-browser-qa/node_modules node tests/browser/ledger.cjs /tmp/noname-ledger-demo.json
```

浏览器脚本只接受明确标记的临时 demo。可用 `NONAME_PYTHON` 指定自己的 Python 路径。演示服务无活动半小时后退出；每次验收生成新 demo，避免前一次模拟审核改变测试起点。

插件仓库运行 `npm run build` 和 `npm test`。GitHub 同步时，先提交主仓共享源码，再用 `sync:kernel -- --commit <sha>` 从该精确提交导出插件快照，同时更新 submodule pin 与 `KERNEL_VERSION.txt`。提交插件前必须通过 `npm run check:kernel`，验证源码、快照与来源声明一致。开发阶段未提交的 overlay 不能冒充原 pin 对应的内容，也不应直接用旧 pin 覆盖新展示。

## 设计参考

产品判断以[首次愿景](vision.md)为准。辅助参考 [Letta 的 memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks) 对持久上下文用途的明确描述，以及 [GitHub Primer 的可访问性检查项](https://primer.style/accessibility/tools-and-resources/checklists/) 对内容层级、标签和键盘操作的要求。NoName 继续保留自己的人工审核与不可改写来源边界。
