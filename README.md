# NoName Agent Harness

> 一个把「上下文」当作资产的 agent harness —— agent 与你的关系不是一次次会话，而是一本持续复利的账本。

**当前状态：概念与设计阶段。** 尚未开始实现，也不急于实现。

## 文档地图

| 文件 | 内容 |
|---|---|
| [docs/vision.md](docs/vision.md) | 愿景与核心理念：它是什么、为谁而做、四条原则（先读这个） |
| [docs/architecture.md](docs/architecture.md) | v0.2 总体架构：证据、记忆、品味、上下文包、插件、多模型与验证顺序 |
| [docs/memory-model.md](docs/memory-model.md) | 可溯源记忆：append-only 证据、source span、版本化失效、审核和检索投影 |
| [docs/runtime-architecture.md](docs/runtime-architecture.md) | 工程骨架：模型适配器、模型配方、工具注册表、会话日志、agent loop、插件运行时 |
| [docs/ledger.md](docs/ledger.md) | 交互式账本：时间线、因果图、状态 diff 和审核收件箱 |
| [docs/taste-cards.md](docs/taste-cards.md) | 品味双轨与多模态卡片：Authored Taste、Adopted Taste、复核与视觉风险 |
| [site/index.html](site/index.html) | 产品介绍页（manifesto）。单文件、无依赖，双击即可在浏览器打开 |

## 原则速览

1. **证据不可丢，记忆可投影** —— 压缩可以有损，原始证据必须可回放、可溯源。
2. **上下文包是视图，不是数据库** —— 每个模型和任务都可组装不同上下文，但指向同一份事实来源。
3. **规范与品味都必须审核** —— 法典写入是长期影响的签署行为；品味分为自述与采纳两条来源轨道。
4. **核心接口稳定，能力可以替换** —— 模型、工具、workflow 和插件都可演进，但事件、权限、版本和迁移有长期契约。
5. **账本是地图，不是控制面板** —— 让人看见发生了什么、为什么发生、现在的状态从哪里来。

> 哪怕最后只有一个人在用，也愿意把它做出来。
