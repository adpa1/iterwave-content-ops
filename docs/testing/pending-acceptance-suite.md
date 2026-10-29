# IterWave 待测试验收脚本与清单

基于当前 `main` 分支源码和 Issue #32 整理。本文区分**当前可自动执行的回归测试**、**需要部署环境执行的人工验收**和**必须等热切换实现后才能执行的验收**。静态检查不能替代运行时隔离测试。

## 0. 当前事实与验收红线

1. `RoundExecutor._auto_schedule()` 只写入 `drafts.scheduled_publish_at`；`DraftSaver` 只保存草稿，明确禁止点击发布按钮。因此当前“排期”不是系统自动发布队列。
2. 账号组的数据库、浏览器 profile、storage state、登录二维码/状态文件应位于各自租户目录。
3. `SchedulerEngine.shutdown()` 使用 `shutdown(wait=False)`，不会等待所有在途 job 结束。
4. APScheduler job wrapper 通过模块级 `_ACTIVE_ENGINE` 选择引擎。新旧 scheduler 重叠时存在旧任务调用新引擎的风险。
5. **在 Issue #32 的 runtime 生命周期重构和隔离测试通过前，不得启用真正的运行时热切换。**

## 1. 一键执行当前可运行的自动化回归

在仓库根目录运行：

```bash
python -m pip install -e '.[dev]'
bash scripts/run_pending_acceptance_tests.sh
```

也可直接运行完整后端测试：

```bash
pytest -q
```

脚本覆盖现有租户目录隔离、登录路径隔离、排期不等于发布、账号身份登记、全局统计、媒体推荐和调度配置相关测试。脚本不需要真实平台账号、API 密钥或线上数据库。

## 2. 自动化测试用例清单

| ID | 测试点 | 自动化入口 | 通过标准 |
|---|---|---|---|
| TEN-001 | 租户 A/B 数据库路径隔离 | `tests/test_tenant_registry.py`、`tests/test_tenant_switch_acceptance.py` | A/B 数据库路径不同，互相读不到对方探针数据 |
| TEN-002 | A/B 登录目录隔离 | `tests/test_tenant_switch_acceptance.py` | 二维码、状态文件、profile、storage state 均使用各自目录 |
| QUEUE-001 | 排期只写计划时间 | `tests/test_tenant_switch_acceptance.py` | 设置 `scheduled_publish_at`，状态仍是 `draft_saved` |
| QUEUE-002 | 有排期不代表已发布 | `tests/test_tenant_switch_acceptance.py` | 不得自动改成 `published` 或 `user_published` |
| ACC-001 | 同平台账号标识去重 | `tests/test_platform_accounts.py` | 同平台大小写/规范化后重复返回冲突，不同平台允许相同 handle |
| ACC-002 | 账号登记 API | `tests/test_platform_accounts.py` | 创建、列表、重复登记行为符合 API 契约 |
| MON-001 | 全局看板跨租户聚合 | `tests/test_global_monitor.py` | 租户内容、草稿、素材统计准确，接口只读 |
| MEDIA-001 | 关键词推荐只选已审核素材 | `tests/test_media_recommendations.py` | 未审核/无关素材不进入推荐结果；不自动分配 |
| LOGIN-001 | 登录进程命令与状态安全 | `tests/test_account_login.py` | profile 名称匹配、状态校验、敏感 URL 脱敏、验证码校验 |
| SCHED-001 | 定时任务配置和去重 | `tests/test_scheduler_config_and_dedup.py`、`tests/test_scheduler_jobs.py` | 调度配置有效；去重及任务登记符合预期 |

## 3. 热切换实现后必须新增的自动化测试

这些是**验收要求，不代表当前代码已经支持**。热切换实现前，先让测试与新的 runtime manager/API 契约绑定；不得为了让测试通过而 mock 掉隔离逻辑本身。

| ID | 场景 | 操作 | 预期结果 |
|---|---|---|---|
| HOT-001 | A→B→A | 在 A 写入标记，切 B 写入另一标记，再切回 A | A/B 标记各自保留且互不可见 |
| HOT-002 | DB 读写隔离 | A 请求开始后并发切 B，A 请求继续写入 | A 的请求只写 A DB；切换期间新业务请求被 gate 阻断或明确拒绝 |
| HOT-003 | 在途 scheduler job | A job 暂停在可控 barrier，触发切换 B | 切换不得在 A job 结束前释放/替换其依赖；A job 不得读取 B runtime |
| HOT-004 | 旧 scheduler 停止 | A scheduler 含 interval job，切换 B | A 不再启动新 job；所有在途 job 已排空或按明确取消协议终止 |
| HOT-005 | 全局引擎串租防护 | A job wrapper 等待期间启动 B runtime | A job 仍绑定 A runtime generation，不能通过共享可变全局变量转到 B |
| HOT-006 | 切换初始化失败 | 注入 B 数据库迁移/adapter 初始化异常 | 原 A runtime 恢复可用；不残留半初始化的 B runtime |
| HOT-007 | 并发切换 | 两个请求同时发起 A→B 与 A→C | 只允许一个切换成功；最终 runtime 状态唯一且一致 |
| HOT-008 | 并发业务请求 | 切换开始后提交读、写、素材上传请求 | 请求 gate 行为确定；不能在一半请求使用 A、一半依赖使用 B |
| HOT-009 | 登录子进程 | A 扫码登录进程运行中时请求切换 | 切换要么等待进程安全结束，要么明确拒绝；不可将 A 的状态写进 B 目录 |
| HOT-010 | 数据库关闭时序 | 切换期间在途请求持有 A 连接 | 连接只能在所有使用者退出后关闭；无 closed connection/写入丢失 |
| HOT-011 | 调度器数据库文件 | A/B scheduler 使用各自 jobstore | jobstore 路径分离；A 的持久化任务不出现在 B 中 |
| HOT-012 | 切换后状态刷新 | 前端切换成功/失败 | 成功刷新租户数据；失败保留原租户并显示可理解错误 |
| HOT-013 | A 队列保留 | A 存在待处理草稿，切 B，再切回 A | A 的草稿和排期时间原样保留；切换本身不删除、不迁移、不发布 |
| HOT-014 | 发布状态不确定 | 将来真实自动发布已发出请求但未拿到响应 | 标记“待核验”，不能盲目重试造成重复发布 |
| HOT-015 | 运行时回滚 | 切换完成前在 adapter 或 notifier 初始化失败 | runtime 指针、数据库连接、scheduler、登录目录全部回到 A 的一致状态 |

建议用 `asyncio.Event` 或测试 barrier 控制并发时序；不要用固定 sleep 作为唯一同步手段。数据库断言必须直接检查 A、B 两个独立文件，不能只检查 API 返回的租户 ID。

## 4. AI 媒体功能的补充验收

这些场景需要在对应 API 测试中覆盖；当前已有关键词推荐测试，但不能据此宣称真实视觉模型和图片生成服务已通过验收。

| ID | 场景 | 预期结果 |
|---|---|---|
| AI-MEDIA-001 | 未启用 `ITERWAVE_ENABLE_VISION_MEDIA_RANKING` | 不调用视觉 API，回退关键词匹配 |
| AI-MEDIA-002 | 已启用但缺少 `OPENAI_API_KEY` | 不泄露密钥，不调用外部服务，安全回退 |
| AI-MEDIA-003 | 视觉 API 超时/异常/非法 JSON | 返回可用的关键词回退结果，记录安全日志 |
| AI-MEDIA-004 | 候选素材超过上限 | 最多发送约定数量的候选图（当前实现上限 8） |
| AI-MEDIA-005 | 候选素材处于待审核或已禁用状态 | 不发送给视觉服务，不出现在推荐结果 |
| AI-MEDIA-006 | 视觉服务返回推荐 | 返回推荐理由，但不得自动分配素材 |
| AI-GEN-001 | 未配置图片生成服务/密钥 | 前端按钮禁用或 API 返回可理解错误 |
| AI-GEN-002 | 生成成功 | 生成素材写入当前租户，进入待审核状态 |
| AI-GEN-003 | 生成失败、超时或额度不足 | 无伪造成功记录；错误信息不包含密钥 |
| AI-GEN-004 | 审核前尝试分配 | 未审核生成图不得作为已批准素材使用 |
| AI-GEN-005 | A 租户生成素材后切 B | B 看不到 A 的生成素材，A 切回后仍可见 |

视觉排序会把候选图片与内容文本发送给配置的视觉 API。只有在显式启用并完成数据授权确认后，才可在有真实密钥的测试环境中执行联调。

## 5. 需要部署环境执行的人工验收

1. 在测试账号组 A 中登录一个平台账号，确认登录状态和浏览器 profile 文件落在 A 的数据目录。
2. 在 A 中生成草稿并设置计划发布时间，记录草稿 ID、状态和计划时间。
3. 选择 B。当前版本应明确提示需要重启生效，不能显示“已即时切换成功”。
4. 在不执行重启的情况下确认运行中的业务仍绑定原 runtime；不要把 UI 中已选的 B 当成 runtime 已切换。
5. 使用隔离测试数据验证全局监控的租户汇总和账号登记数量；不能把未登记的会话数推算成真实平台账号数。
6. 只有在未来热切换自动化测试全绿后，才进行真实平台的 A→B→A 账号登录和发布链路验证。不要用真实发布按钮作为第一轮测试；当前设计红线是保存草稿而非自动发布。

## 6. 验收通过门槛

- 当前回归脚本全部通过。
- 热切换 15 项测试全部实现并通过，且不能仅靠 mock 证明隔离。
- AI 视觉与生成服务的错误路径、审核路径和租户隔离测试通过。
- 前端构建通过；部署环境人工验收完成。
- 记录 CI 运行链接、测试环境、commit SHA 和未覆盖项。
- 上述门槛未达成前，结论只能写“部分测试通过”，不能写“租户热切换安全可用”。
