# IterWave — AI 图文内容运营工作台

> **Early Preview — 设计方案 + 可交互原型 + 持续开发中的开源项目**

从热点发现、图文生产，到发布复盘，让内容持续迭代。

IterWave 是一款面向多平台图文创作者的开源内容运营工作台，帮助用户将选题发现、总体计划、平台分项计划、AI 图文生产、质量检查、人工发布确认与数据复盘串联成可追溯的工作流。

---

## 核心能力

- **发现与策划** — 结合账号定位、选题库与相关热点，形成可编辑的内容计划
- **多平台图文生产** — 为各平台生成适配的文案和图片卡片，并执行质量检查
- **可编辑的 Agent 编排** — 调整工作流步骤、前置依赖和人工审核节点，并做流程诊断
- **人工把关** — 内容自动整理至草稿，发布与关键决策保留人工控制
- **数据驱动迭代** — 记录逐篇指标、明确数据缺失状态，复盘结论自动回流下一轮策略
- **自动闭环（自生长）** — 复盘结论进入选题评分与文案提示词，条件满足时自动开下一轮（仍只存草稿）
- **灵活接入选题库** — 上传 CSV / Excel / JSON / SQLite，或填远程 CSV / JSON / RSS 地址按天同步，也可直接用服务器路径
- **行业与热点可配置** — 行业模板一键填入画像与合规红线；热点支持综合热榜 + 自定义 RSS 源
- **可部署到任意服务器** — 不绑定本机路径、字体自动探测、支持子路径部署与访问口令
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

## 最近更新（2026-10-29）

- **访问保护** — 设置访问口令后页面需登录、`/api/*` 统一校验；未设置时顶部常驻警告。脚本可用 `X-Access-Token` / `Authorization: Bearer` 调用，登录失败按 IP 限速
- **选题库三种来源** — 页面上传（CSV / Excel / JSON / JSONL / SQLite，≤20MB）、远程地址（CSV / JSON / RSS，可带鉴权，每天自动同步）、服务器路径（相对或绝对）。字段自动识别 + 下拉映射 + 预览；不接选题库也能用热点转入和手动添加
- **部署可移植** — Linux / Windows 中文字体候选 + `ITERWAVE_FONT` 覆盖；企业微信 / 飞书 Webhook 可在页面填写（只进 `.env`）；前端支持 `VITE_BASE` 子路径构建
- **行业模板** — 通用 / 知识科普 / 生活方式 / 财经 / Web3 / 健康，一键填入画像、维度与合规红线，随时可改
- **热点源可配置** — 综合热榜可关闭，支持添加行业 RSS 源；采集失败显示中文提示，不用假数字填充
- **账号与登录** — 四平台扫码登录页，登录态持久化为本地 storage state，支持短信验证码登录
- **自动闭环** — 复盘结论（维度加减分、各平台文案要点）回流下一轮，条件满足自动开轮、目标达成自动收手（自动轮只存草稿）
- **选题池与内容管理** — 热点转入、手动录入、知识库选题统一管理，支持软删除与恢复；已发布内容禁止删除以保护复盘数据
- **全局搜索** — 跨页面搜索选题 / 内容 / 轮次，直接深链到对应页面与记录
- **新手引导** — 11 项自检（选题来源、字体、通知、口令等），未通过项一键跳转处理；没有阶段目标也能「试跑第一轮」
- **体验修复** — Dashboard 旧任务数据过滤与空态、移动端顶栏溢出、全局搜索快捷键、提示文案产品化（去内部代号）

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

小红书 · 抖音 · 快手 · 视频号

> 视频号网页端仅支持视频发布，图文相关的存草稿与评论互动能力受限，系统会直接给出明确提示，不会假装成功。

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

## 部署到服务器（任意位置）

IterWave 不绑定任何电脑或目录，Linux / macOS / Windows 都可以部署：

1. **中文字体**（出图必需）：Debian/Ubuntu `sudo apt install fonts-noto-cjk`，CentOS `sudo yum install google-noto-sans-cjk-ttc-fonts`；
   或在 `config/.env` 写 `ITERWAVE_FONT=/path/to/font.ttc`。启动后「新手引导 → 检查清单」会显示用的是哪个字体。
2. **子路径部署**（如 `https://域名/iterwave/`）：`cd web && VITE_BASE=/iterwave/ npm run build`，反向代理把 `/iterwave/` 转发到后端根路径。
3. **访问口令**：公网部署后第一件事，在页面顶部的提示条或「系统设置 → 通知与访问」设置口令（保存在 `config/.env` 的 `ITERWAVE_ACCESS_TOKEN`）。
   忘记口令：改 `.env` 后重启服务。脚本调用接口可带 `Authorization: Bearer <口令>`。
4. **选题库**：「系统设置 → 选题库」三选一 —— 上传文件（CSV / Excel / JSON / JSONL / SQLite）、远程地址（CSV / JSON / RSS，每天自动同步）、
   服务器路径（相对项目目录或绝对路径）。也可以不接，只用热点转入和手动添加的选题。
5. **通知**：在「系统设置 → 通知与访问」粘贴企业微信 / 飞书群机器人 Webhook；macOS 本机通知只在 macOS 上可用。
6. **行业**：平台不绑定垂类。「目标与画像」可从行业模板（通用 / 知识科普 / 生活方式 / 财经 / Web3 / 健康）一键填入画像与合规红线；
   「热点雷达 → 筛选逻辑 → 热点来源」可关闭综合热榜、添加行业 RSS 源。

## 项目结构

```
iterwave-content-ops/
├── src/
│   ├── core/          # 共享内核：状态机、评分、螺旋引擎、熔断、调度、选题库、访问保护...
│   ├── adapters/      # 四平台适配器（基于 Playwright 的存草稿器 + 数据读回）
│   ├── vendor/easel/  # 从 Easel（Apache 2.0）复制的登录态、数据采集代码
│   └── api/           # FastAPI 路由（main.py 业务 + auth.py 访问保护）
├── web/               # React 前端
├── migrations/        # 18 个 DB schema 迁移
├── tests/             # pytest（148 例）
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

> **非空代码**：Python 27,300+ 行（`src/`）· TypeScript 11,800+ 行（`web/src/`）· pytest 2,480 行 / 148 例全部通过 · 18 个 DB 迁移脚本 · 103 条 API 路由
>
> 所有模块均有实质性实现，不是空壳。四平台存草稿已按平台分别真机验证；评论回复与完整数据闭环仍需真实账号和已发布作品才能验收（见「已真机验证 / 尚未验证」）。

### 后端核心（src/core/）

| 模块 | 文件 | 行数 |
|---|---|---|
| 配置加载 + Pydantic v2 模型 | config.py | 550 |
| 配置白名单 / 覆盖 / 审计 | config_store.py | 799 |
| SQLite + schema 迁移 | db.py | 386 |
| 两层状态机（轮次 9 态 + 单篇 8 态） | state_machine/__init__.py | 292 |
| 选题评估引擎（冷启动/样本积累/稳定运行三模式） | scoring/* | 361 |
| 终极目标检查器（方向性约束） | guard/* | 198 |
| 螺旋引擎 + 缺口分析 | spiral/__init__.py | 285 |
| 运营引擎（评论/关注） + 熔断 + 节奏控制器 | ops/* | 719 |
| APScheduler 调度器 | scheduler/__init__.py | 1,220 |
| 内容管线 + 轮次执行器 | content_pipeline.py + round_executor.py | 1,402 |
| 自动闭环（自生长） | autogrow.py | 494 |
| Agent 编排 + 流程诊断 | orchestration.py | 381 |
| 选题池 + 内容删除规则 | topic_pool.py + content_admin.py | 300 |
| 选题库来源 / 动态读取 | kb_sources.py + kb_reader.py | 691 |
| 账号扫码登录 + 登录态管理 | account_login.py + login_manager.py + _login_worker.py | 721 |
| 访问口令 + 会话 + 限速 | auth.py | 213 |
| 脱敏 / .env 密钥写入 | redact.py + secrets_env.py + env_store.py | 166 |
| 行业模板 + 画像 + 目标口径 | verticals.py + industry_presets.py + persona.py + goal_progress.py | 632 |
| 热点发现 + 匹配 + 去重 + 异步服务 | hot_topics.py + hot_topic_matcher.py + hot_topic_dedup.py + hot_topic_service.py | 1,037 |
| 归一化 z-score + 洞察 | metrics/* + insights.py | 515 |
| 计划版本签名（§19 内容一致性） | plan_version.py | 344 |
| 断点恢复（幂等 + pending_verify） | recovery.py | 151 |
| 通知（7 场景 × 4 级） | notify/* | 342 |

### 平台适配器（src/adapters/）

| 适配器 | 行数 | 说明 |
|---|---|---|
| 小红书 | 326 | 存草稿 + 数据读回 |
| 抖音 | 287 | 存草稿 + 数据读回 |
| 快手 | 277 | 存草稿 + 数据读回 |
| 视频号 | 77 | 网页端仅视频，图文能力明确拒绝 |
| 平台基类 + 数据读回 | 480 + 398 | 四平台统一接口 |
| 存草稿器（红线：永不点发布） | 907 | 等平台成功信号，无证据标记人工核实 |

### API 层

`src/api/main.py` — 2,853 行、99 条业务路由；`src/api/auth.py` — 4 条访问保护路由。覆盖状态机、选题、评分、运营、熔断、配置、通知、选题库、编排、账号登录、自动闭环等。

### React 前端（web/）

36 个文件 11,800+ 行，13 个页面：Dashboard · HotTopics · Goals · Production · Calendar · Assets · Operations · Analytics · Gaps · Agent · Accounts · Settings · Onboarding，以及 Sidebar、AuthGate、GlobalSearch、VerticalPicker、AutogrowPanel、TopicPoolPane、CreatedContentPane、PersonaMatrix、GoalHero 等 12 个组件。

### 测试

16 个 pytest 文件 2,480 行、148 例全部通过，覆盖状态机、三模式评分、NULL vs 0 红线、熔断恢复、调度任务、API 端点、通用平台、自动闭环、账号登录、热点异步采集。前端 `tsc && vite build` 通过。

### 已真机验证 / 尚未验证

已按平台真机验证存草稿：快手服务端草稿保存成功，抖音与小红书在浏览器本地保存，视频号网页端不支持图文（明确返回提示）。

尚未验证：

- 评论回复的成功回写（账号暂无已发布作品，幂等二次调用未实测）
- 四平台数据采集的长期稳定性（需要已发布内容积累）
- 完整螺旋闭环（发布 → 数据 → 复盘 → 策略 → 下一轮）在真实账号上的完整跑通

## License

本项目基于 Apache License 2.0 开源，继承 Easel 上游许可证。
