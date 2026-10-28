# IterWave — AI 图文内容运营工作台

> **Early Preview — 设计方案 + 可交互原型 + 持续开发中的开源项目**

从热点发现、图文生产，到发布复盘，让内容持续迭代。

IterWave 是一款面向多平台图文创作者的开源内容运营工作台，帮助用户将选题发现、总体计划、平台分项计划、AI 图文生产、质量检查、人工发布确认与数据复盘串联成可追溯的工作流。

---

## 核心能力

- **发现与策划** — 结合账号定位、选题库与相关热点，形成可编辑的内容计划
- **多平台图文生产** — 为各平台生成适配的文案和图片卡片，并执行质量检查
- **可编辑的 Agent 编排** — 调整工作流步骤、前置依赖和人工审核节点
- **人工把关** — 内容自动整理至草稿，发布与关键决策保留人工控制
- **数据驱动迭代** — 记录逐篇指标、明确数据缺失状态，利用复盘结果指导下一轮内容策略
- **本地优先** — 以用户可控的知识库、账号画像和内容资产为基础，逐步构建可配置、可审计的运营闭环

## 产品定位

IterWave 聚焦于**图文运营**场景，与通用 AI Agent 内容生成工具的核心差异：

1. **专注图文** — 不把视频、配音、音乐生成当作产品主线，深耕选题、平台化文案、图片卡片、质量检查与运营复盘
2. **可调整的工作流** — 用户可以修改编排、平台分项计划、依赖关系和人工审核节点，而不只是调用预设技能
3. **人工把关的闭环** — 自动发现、计划、创作、存草稿；发布由用户确认，再回收数据、分析原因、生成下一轮策略。强调过程透明、状态可追溯
4. **本地优先与可解释复盘** — 用户掌握自己的选题库、账号画像、内容版本和运营数据；每个策略结论能查看依据、样本量、置信度和人工修改记录

## 工作模式

```
选题库 + 账号定位 + 热点发现
           ↓
    内容计划（可编辑的 Agent 编排）
           ↓
    四平台文案生成 + 图片卡片制作
           ↓
    终极目标检查 + 质量门 → 存草稿
           ↓
    ✅ 用户手动发布（永不自动点发布）
           ↓
    数据采集 → 复盘 → 螺旋引擎 → 下一轮策略调整
```

**红线**：Playwright 只存草稿，永不自动点击"发布"按钮。发布确认通过 UI 手动操作或定时只读轮询。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ / TypeScript |
| 后端 | FastAPI + SQLAlchemy + Pydantic v2 |
| 调度 | APScheduler（SQLiteJobStore 持久化） |
| 浏览器 | Playwright（仅存草稿 + 只读数据采集） |
| 图片 | Pillow + OpenCV + NumPy |
| 前端 | React 18 + Vite |
| 数据库 | SQLite |

## 支持平台

小红书 · 抖音 · 快手 · 视频号（仅图文）

## 快速开始

### 后端

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# 复制 config/config.yaml 中的配置，填入 API Key（存 config/.env，已 gitignore）
uvicorn src.api.main:app --reload
```

### 前端

```bash
cd web
npm install
npm run dev
```

## 项目结构

```
iterwave-content-ops/
├── src/
│   ├── core/          # 共享内核：状态机、评分、螺旋引擎、熔断、调度...
│   ├── adapters/      # 四平台适配器（基于 Playwright 的存草稿器 + 数据读回）
│   ├── vendor/easel/  # 从 Easel（Apache 2.0）复制的登录态、数据采集代码
│   └── api/           # FastAPI 路由
├── web/               # React 前端
├── migrations/        # DB schema 迁移
├── tests/             # pytest
└── config/            # 主配置 + .env（密钥已 gitignore）
```

## 与 Easel 的关系

IterWave 的部分平台适配器代码基于 [Easel](https://github.com/kaichen/Easel)（Apache License 2.0）复制改写：

- 登录态管理、二维码 DOM 渲染签名、创作者中心数据采集路径 → 复制进 `src/vendor/easel/`
- 四平台发布器 → 按需复制后改造成存草稿模式（移除自动发布按钮）
- **核心模块独立实现**：选题评估引擎、螺旋引擎、缺口分析、熔断机制、节奏控制器、两层状态机

详见 `src/vendor/easel/LICENSE` 和 `src/vendor/easel/NOTICE`。

## 安全边界

- API Key、Cookie、Token 只存本地 `.env` / 登录态文件，已 `chmod 600`
- 日志中的敏感信息脱敏（保留前 4 位 + `***`）
- Playwright 永不点击"发布"按钮
- 主动评论、关注等操作极低配额，崩溃场景标记 `pending_verify` 待用户核实
- 熔断机制：检测到平台异常自动停止所有运营，3 步验证后方可恢复

## 当前进度

| 阶段 | 状态 |
|---|---|
| 设计方案 v1.3 | ✅ 完成 |
| Demo 原型 | ✅ 可交互 |
| 状态机 + DB schema | ✅ 迁移脚本就绪 |
| 平台适配器 | 🚧 开发中 |
| 选题评估 + 螺旋引擎 | 🚧 开发中 |
| React 前端 | 🚧 开发中 |

**交互原型中的模拟操作不代表真实平台连接、模型调用或数据采集已经完成。**

## 路线图

- Phase 1 — 项目骨架 + DB schema + 配置模块
- Phase 2 — 平台适配器接口 + 存草稿器
- Phase 3 — 选题评估 + 文案生成 + 终极目标检查
- Phase 4 — 状态机 + APScheduler + 断点恢复
- Phase 5 — 数据拉取 + 归一化 + 螺旋引擎
- Phase 6 — 运营引擎 + 熔断 + 节奏控制
- Phase 7 — React 前端

## License

本项目基于 Apache License 2.0 开源，继承 Easel 上游许可证。
