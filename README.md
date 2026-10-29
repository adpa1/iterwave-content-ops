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

### 首次访问与公网部署

- 首次启动后先完成访问口令设置；未设置口令时，业务 API 默认不应对未认证访问开放。
- 本机开发如需免引导口令完成首次设置，必须显式设置 `ITERWAVE_ALLOW_LOCAL_BOOTSTRAP=1`，且使用本机直连地址。默认情况下，本机也需引导口令。公网或反向代理部署前，先在服务端环境变量中设置强随机的 `ITERWAVE_BOOTSTRAP_TOKEN`，再通过首次设置页面提交该引导口令并设置正式访问口令。
- 公网部署不要启用 `ITERWAVE_ALLOW_LOCAL_BOOTSTRAP=1`。不要把引导口令写进仓库、前端配置或日志；完成初始化后应从部署环境中移除引导口令。
- 配置文件与数据库目录应限制为运行服务的操作系统用户可读写，并通过 HTTPS 访问管理界面。

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

## 代码现状（Early Preview）

> **非空代码**：Python 10,600+ 行 · TypeScript 5,700+ 行 · pytest 1,088 行 · 9 个 DB 迁移脚本
>
> 所有模块均有实质性实现，不是空壳。但真实平台连接（Playwright）、模型调用链路、端到端集成尚未在四平台完整验证。

### 后端核心（src/core/）

| 模块 | 文件 | 行数 |
|---|---|---|
| 配置加载 + Pydantic v2 模型 | config.py | 351 |
| SQLite + schema 迁移 | db.py | 241 |
| 两层状态机（轮次 9 态 + 单篇 8 态） | state_machine/__init__.py | 279 |
| 选题评估引擎（冷启动/样本积累/稳定运行三模式） | scoring/topic_eval.py | 333 |
| 终极目标检查器（方向性约束） | guard/ultimate_goal_checker.py | 176 |
| 螺旋引擎 + 缺口分析 | spiral/__init__.py | 285 |
| 运营引擎（评论/关注） + 熔断 + 节奏控制器 | ops/engine.py + circuit_breaker.py + rhythm.py | 590 |
| APScheduler 调度器（6 类任务） | scheduler/__init__.py | 1,072 |
| 内容管线 + 轮次执行器 | content_pipeline.py + round_executor.py | 1,065 |
| 热点发现（60s.viki.moe 公益 API） | hot_topics.py | 362 |
| 归一化 z-score + 洞察 | metrics/__init__.py + insights.py | 514 |
| 计划版本签名（§19 内容一致性） | plan_version.py | 227 |
| 断点恢复（幂等 + pending_verify） | recovery.py | 151 |
| 目标进度 + 终极目标口径 | goal_progress.py | 118 |

### 平台适配器（src/adapters/）

| 适配器 | 行数 | 说明 |
|---|---|---|
| 小红书 | 321 | 存草稿 + 数据读回 |
| 抖音 | 277 | 存草稿 + 数据读回 |
| 快手 | 278 | 存草稿 + 数据读回 |
| 视频号 | 77 | 存草稿（简化版）|
| 平台读回（创作者中心数据采集） | 278 | 四平台统一接口 |
| 存草稿器（红线：永不点发布） | — | draft_saver.py |

### API 层

`src/api/main.py` — 1,645 行 FastAPI 路由，覆盖状态机、选题、评分、运营、熔断、配置、通知等全部端点。

### React 前端（web/）

19 个文件 5,758 行，含 11 个页面：Dashboard · Production · HotTopics · Gaps · Goals · Calendar · Assets · Agent · Analytics · Operations · Settings，以及 Sidebar、AppContext、api 封装、DemoBadge（原型边界标注）等组件。

### 测试

8 个 pytest 文件 1,088 行，覆盖状态机、三模式评分、NULL vs 0 红线、熔断退出路径、调度器任务、API 端点、适配器接口。

### 尚未验证

- Playwright 真实四平台存草稿链路（端到端）
- 模型调用链路（文案生成 + 终极目标检查 + 图片卡片制作）
- 四平台数据采集 API 真实连通性
- 完整螺旋闭环（发布 → 数据 → 复盘 → 策略 → 下一轮）

## License

本项目基于 Apache License 2.0 开源，继承 Easel 上游许可证。
