# 配置中心改造方案（ADR）

- 状态：已批准进入实施
- 基准：2026-10-30 main
- 实施分支：feat/config-center-foundation-20261030
- 范围：账号组运行时配置、配置元数据、配置审计、旧 JSON 兼容迁移
- 原则：先兼容迁移，再切换读取；不在本阶段迁移运营目标、行业模板或算法策略

## 1. 当前问题

当前配置存在四种来源：config.yaml、Pydantic 模型默认值、config_overrides.json、Python 白名单常量。在线修改写入 JSON，SQLite 的 config_changes 只记录审计。前端又维护一份配置入口。结果是默认值、可编辑范围和运行时状态可能漂移。

## 2. 配置分类与归属

| 分类 | 归属 | 示例 |
|---|---|---|
| 部署配置 | YAML / 环境变量 | 数据目录、监听端口、部署级开关 |
| 账号组运行时配置 | 账号组 SQLite | 节奏、评分权重、热点阈值、任务计划 |
| 敏感凭据 | 环境变量 / 密钥存储 | LLM API Key、Webhook URL |
| 业务数据 | 账号组 SQLite 业务表 | 主动评论目标、关注目标、用户模板 |
| 系统安全约束 | 代码 | 强制门禁、路径校验、文件类型白名单 |
| 算法实现 | 代码 | 评分公式、平台协议、状态机 |
| 算法策略参数 | 版本化配置 | 评分权重、语义匹配阈值、质量评分权重 |

## 3. 配置优先级

从低到高：

1. Pydantic 模型默认值（仅用于缺失字段的安全兜底）
2. config.yaml（部署初始值）
3. 账号组 SQLite 当前配置（用户在线修改）
4. 环境变量仅覆盖明确声明为部署级或敏感凭据的字段；不得用任意环境变量隐式覆盖账号组业务策略

安全硬锁不参与优先级合并：任何来源都不能关闭。

## 4. 数据模型

### tenant_settings

保存账号组当前有效覆盖值。

- key TEXT PRIMARY KEY
- value_json TEXT NOT NULL
- value_type TEXT NOT NULL
- version INTEGER NOT NULL
- updated_at TEXT NOT NULL
- updated_by TEXT NULL

### tenant_setting_versions

保存每次配置变更后的版本快照。

- id INTEGER PRIMARY KEY
- version INTEGER NOT NULL
- snapshot_json TEXT NOT NULL
- changed_at TEXT NOT NULL
- changed_by TEXT NULL
- change_reason TEXT NULL
- UNIQUE(version)

现有 config_changes 保留为字段级审计表，但 old_value/new_value 在写入前必须脱敏；版本快照不得包含 API Key、Webhook 凭据、URL userinfo 等凭据。为兼容现有 RSS 私有源，带签名查询参数的源 URL 仍保留在当前配置值中供采集使用，但审计、版本快照和诊断 API 必须脱敏；数据库文件权限限制为 0600。

## 5. 统一配置元数据

本阶段以 config_store.py 中现有后端白名单与校验器为权威定义，新增 GET /api/config/metadata 从同一校验规则生成字段元数据。现有前端仍保留强类型编辑组件；后续再逐步消费元数据，避免一次性改造全部表单。

每项元数据包含：

- key、name、group、type
- default、min、max、enum
- tenant_scoped、editable、restart_required
- sensitive、description
- validator / dependency（由代码注册，不接受用户提供可执行内容）

元数据只负责描述和展示；实际校验仍调用明确的类型/业务校验器。不得通过元数据把安全硬锁变成可编辑项。

## 6. 旧 JSON 迁移与兼容

迁移必须幂等：

1. 数据库迁移创建 tenant_settings、tenant_setting_versions。
2. 初始化时检测 config_overrides.json。
3. 对每个键执行现有白名单校验；拒绝未知键和非法值，不静默导入。
4. 在单个 SQLite 事务中导入所有合法值，并写入初始版本快照。
5. 只有事务成功且回读校验一致，才写入迁移完成标记。
6. 迁移标记存在时，数据库为在线配置唯一读写源；旧 JSON 保留为只读回滚备份，不再被覆盖。
7. 如果迁移失败，继续使用旧 JSON，不能丢弃用户配置。
8. 提供诊断信息：导入数量、拒绝数量、拒绝原因（不打印敏感值）。

配置备份/恢复必须在迁移过渡期同时兼容旧 config_overrides.json 与新数据库配置；新版本备份应包含数据库中的配置表。

## 7. 审计与事务边界

一次批量 PATCH 必须满足：

- 全量校验通过后才写入；
- 配置值、版本快照、字段级审计在同一事务提交；
- 提交成功后再更新内存 Config 对象并触发 scheduler 重排；
- 任一写入失败时回滚，不出现“内存已更新、持久化失败”；
- 审计写入前对 URL userinfo、token、key、password 等敏感信息脱敏；禁止持久化原始凭据；
- 查询端脱敏继续保留，作为第二道防线。

## 8. 实施阶段

### 阶段 A：基础设施
- 增加 SQLite 迁移表。
- 增加配置元数据注册表与 API 只读输出。
- 增加 DB 配置仓储与 JSON 一次性迁移。
- 保留旧读路径作为失败回退。
- 完成迁移、幂等、回滚、配置优先级和敏感值测试。

### 阶段 B：切换配置读写
- 在线 PATCH 改为 DB 事务。
- 启动时 DB 覆盖 YAML。
- scheduler 和配置页面读取统一配置视图。
- 备份/恢复兼容新旧格式。
- 在至少一个完整版本周期内保留 JSON 只读回滚副本。

### 阶段 C：业务数据迁移
- ops_targets 已迁移到账号组 SQLite 业务表；YAML 仅用于一次性种子回填，运营控制台通过 API 维护。
- 行业模板已迁移到账号组 SQLite；Python 常量仅保留为首次安装种子，支持租户自定义模板版本。
- tenant_registry / active_tenant 的元数据仍待迁移到系统元数据库；本阶段保留 JSON 兼容层，账号组物理目录隔离不变。

### 阶段 D：策略参数治理
- 视觉模型、图片生成、图片质量、热点研判、熔断恢复、备份策略配置化。
- 算法策略增加版本号，素材/研判结果保留策略版本。
- 安全硬上限保留代码控制，配置值只能收紧不能放宽。

## 9. 验收标准

- 旧版 JSON 配置升级后全部保留，合法项值不变。
- 重复启动不会重复导入或生成额外版本。
- 非法/未知配置可诊断，不会被静默应用。
- 数据库不可写时不丢失旧 JSON 配置。
- PATCH 的配置值、版本快照、审计记录原子提交。
- LLM API Key、Webhook 凭据和 URL userinfo 不以原文进入配置表/历史/审计；RSS 私有源签名 URL 仅允许存在当前配置值，历史、审计、日志和 API 响应必须脱敏。
- 账号组之间配置互不可见。
- 安全硬锁在 API、UI 和存储层均不可被覆盖。
- 现有 scheduler 即时生效行为和全部既有测试保持通过。

## 10. 明确不在首批范围

不在同一个 PR 中迁移 ops_targets、行业模板、账号组注册表、图片/评分算法参数。它们依赖配置中心基础设施，拆批可以降低数据迁移和回归风险。
