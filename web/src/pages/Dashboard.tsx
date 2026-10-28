import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import { api, SpiralRound, Goal, HotTopicItem, InsightItem, PlatformProgress, ActionItem } from "../api";
import { platformMeta, Platform } from "../platforms";

/**
 * Dashboard v2 — 动线按审查报告 P0-02/03/04 重构：
 *  1. 取消假总目标%（不同指标不能直接相加）→ 改成按指标分组的进度摘要
 *  2. KPI 四卡：互不重复、导向真实动作（去之前"进行中轮次/复盘/热点"三次重复）
 *  3. "本周期已发布"真实时间窗（用 created_at >= 近 7 天过滤）
 *  4. AgentPipelineStatus 用 status.circuit_broken_platforms 替换 window.__broken_platforms
 *  5. 四平台进度用 platform_progress API 真实分组
 *
 * 动线：先告诉用户该做什么 → 再展示进度与机会
 *    [唯一主操作 + 次操作 + 最近更新时间 + 阻塞告警]
 *    [待我确认] [运行中轮次] [阻塞事项] [本周期已发布]   ← 4 不重复 KPI
 *    [待我处理（主区，按阻塞/截止/影响排序）]  [当前轮次四平台进度]
 *    [热点机会]  [复盘洞察与待决策]
 *    [阶段目标（按指标分组）]  [最近轮次]
 */
export default function Dashboard() {
  const { status } = useApp();
  const [rounds, setRounds] = useState<SpiralRound[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [hottopics, setHottopics] = useState<HotTopicItem[]>([]);
  const [insights, setInsights] = useState<InsightItem[]>([]);
  const [progress, setProgress] = useState<Record<Platform, PlatformProgress>>({} as any);
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const results = await Promise.allSettled([
        api.rounds(),
        api.goals(),
        api.hotTopics().catch(() => ({ items: [] })),
        api.insights().catch(() => ({ items: [] })),
        api.platformProgress().catch(() => ({})),
        api.actionItems().catch(() => ({ items: [] })),
      ]);
      if (results[0].status === "fulfilled") setRounds(results[0].value as SpiralRound[]);
      if (results[1].status === "fulfilled") setGoals(results[1].value as Goal[]);
      if (results[2].status === "fulfilled") {
        const v = results[2].value as any;
        setHottopics(Array.isArray(v) ? v : (v?.items ?? []));
      }
      if (results[3].status === "fulfilled") {
        const v = results[3].value as any;
        setInsights(Array.isArray(v) ? v : (v?.items ?? []));
      }
      if (results[4].status === "fulfilled") setProgress(results[4].value as any);
      if (results[5].status === "fulfilled") {
        const v = results[5].value as any;
        setActions(Array.isArray(v) ? v : (v?.items ?? []));
      }
      if (results.some(r => r.status === "rejected")) setError("部分数据加载失败");
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  // ===== 真实派生数据 =====
  const enabledPlatforms = (status?.enabled_platforms as Platform[]) ?? ["xhs", "douyin", "kuaishou", "wechat_channels"];
  const brokenPlatforms: Platform[] = (status as any)?.circuit_broken_platforms ?? [];
  const hasBlockingIssue = brokenPlatforms.length > 0 || actions.length > 0;

  // KPI 1: 运行中轮次（producing + awaiting_publish，实时快照，不按时间窗）
  const runningNow = rounds.filter(r => ["producing", "awaiting_publish", "created"].includes(r.status)).length;

  // KPI 3: 阻塞事项（直接用 action_items 真实数据，当前后端聚合 4 类：目标超期/轮次超 72h 未确认/熔断冷却到期未 resolved/熔断中）
  const blockingCount = actions.length;

  // KPI 4: 本周期已发布（用 rounds.created_at 过滤最近 7 天；如果有 drafts.user_published_at 更好，但 rounds 没有）
  //    审查报告 P0-04 要求："最近 7 天"必须真按时间筛；否则改名
  const SEVEN_DAYS_MS = 7 * 24 * 3600 * 1000;
  const now = Date.now();
  const thisWeekPublished = rounds.filter(r => {
    const d = new Date(r.created_at).getTime();
    const completed = r.status === "completed" || r.status === "partial_completed";
    return completed && (now - d) <= SEVEN_DAYS_MS;
  }).length;

  // KPI 2: 待我确认的草稿（轮次状态 awaiting_publish 内所有 drafts 都算待确认）
  const awaitingPublishRounds = rounds.filter(r => r.status === "awaiting_publish");
  // 用 action_items 里的 "awaiting_publish_timeout" 类项 + rounds.awaiting_publish 估算
  // 因为后端没直接返回 draft 数，这里用 rounds.awaiting_publish × 平均平台数估算（诚实标注）
  const awaitingConfirmCount = awaitingPublishRounds.length;  // 真实有多少轮在等用户确认发布

  // 阶段目标按 target_metric 分组（审查报告 P0-02：不同指标不能相加 → 按指标分组展示）
  const activeGoals = goals.filter((g: any) => g.status === "active" || !g.status);
  const goalsByMetric = activeGoals.reduce<Record<string, Goal[]>>((acc, g) => {
    const key = (g as any).target_metric ?? "未知";
    (acc[key] ||= []).push(g);
    return acc;
  }, {});

  return (
    <AsyncPanel loading={loading} error={error} onRetry={load} loadingText="加载工作台...">
      {/* ========== 页面头：唯一主操作 + 次操作 + 阻塞告警 ========== */}
      <div className="page-head">
        <div>
          <div className="eyebrow">CONTROL CENTER · 运营总览</div>
          <h1 className="page-title">运营总览</h1>
          <div className="page-desc" style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center" }}>
            <span>最近更新：{new Date().toLocaleTimeString("zh-CN")}</span>
            <span>启用平台：{enabledPlatforms.map(p => platformMeta(p).icon).join(" ")}</span>
            {hasBlockingIssue && (
              <span style={{ color: "#f85149", fontWeight: 600 }}>
                🔴 有 {brokenPlatforms.length + blockingCount} 项阻塞需要处理
              </span>
            )}
            {!hasBlockingIssue && (
              <span style={{ color: "#12b76a" }}>✅ 当前无阻塞</span>
            )}
          </div>
        </div>
        <div className="page-head-actions" style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={load}>↻ 刷新</button>
          <a className="btn" href="?page=hot">🔥 扫描热点</a>
          <a className="btn primary" href="?page=production">🚀 开始新一轮</a>
        </div>
      </div>

      {/* ========== 四个不重复、导向真实动作的 KPI ========== */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 }}>
        <KpiCard
          icon="🔐"
          label="待我确认发布"
          value={awaitingConfirmCount}
          hint="轮次内有草稿等待你点'✅ 我已发布'"
          tone={awaitingConfirmCount > 0 ? "orange" : "gray"}
          href="?page=operations"
        />
        <KpiCard
          icon="⚙️"
          label="运行中轮次"
          value={runningNow}
          hint="生产中 / 待发布"
          tone={runningNow > 0 ? "blue" : "gray"}
          href="?page=production"
        />
        <KpiCard
          icon={blockingCount > 0 ? "⚠️" : "✅"}
          label="阻塞事项"
          value={blockingCount}
          hint={blockingCount > 0 ? "需要你处理的告警/超期/熔断" : "当前无阻塞"}
          tone={blockingCount > 0 ? "red" : "green"}
          href="?page=operations"
        />
        <KpiCard
          icon="📊"
          label="本周期已发布"
          value={thisWeekPublished}
          hint="最近 7 天内完成/部分完成的轮次"
          tone="green"
          href="?page=analytics"
        />
      </div>

      {/* ========== 主工作区：左·待我处理  右·当前轮次四平台进度 ========== */}
      <div className="grid two">
        {/* 左：待我处理（action_items 真实数据，按优先级排序） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">📋 待我处理</div>
              <div className="panel-sub">按阻塞 / 截止时间 / 影响程度排序 · 真实 action_items</div>
            </div>
            <a href="?page=operations" className="btn ghost">处理 →</a>
          </div>
          {actions.length === 0 ? (
            <EmptyHint next="当前没有需要你处理的事项">一切正常 🎉</EmptyHint>
          ) : (
            <div className="list">
              {actions.slice(0, 8).map((it: any, i) => (
                <div key={i} className="task-row">
                  <i className={`task-prio ${severityToTone(it.severity)}`} />
                  <div className="task-main">
                    <div className="task-title">{actionItemTitle(it)}</div>
                    <div className="task-desc" style={{ fontSize: 11 }}>
                      {it.platform ? `${platformMeta(it.platform as Platform).label} · ` : ""}
                      {it.reason ?? "—"}
                      {it.due_at && ` · 截止 ${it.due_at.slice(0, 10)}`}
                    </div>
                  </div>
                  <a href={`?page=operations`} className="btn ghost" style={{ fontSize: 11 }}>去处理 →</a>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 右：当前轮次四平台进度（platform_progress API 真实分组） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">🗺️ 四平台进度</div>
              <div className="panel-sub">平台真实 draft 分组 · 熔断实时状态</div>
            </div>
            <a href="?page=production" className="btn ghost">详情 →</a>
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {enabledPlatforms.map((p) => {
              const m = platformMeta(p);
              const pr: any = (progress as any)[p] ?? { total_drafts: 0, status_breakdown: {}, circuit_broken: false };
              const total = pr.total_drafts ?? 0;
              const cb = brokenPlatforms.includes(p) || pr.circuit_broken;
              const breakdown = pr.status_breakdown ?? {};
              return (
                <div key={p} style={{
                  padding: 10, borderRadius: 8,
                  border: `1px solid ${cb ? "rgba(248,81,73,0.4)" : "var(--border)"}`,
                  background: cb ? "rgba(248,81,73,0.05)" : "var(--panel-alt)",
                  display: "flex", alignItems: "center", gap: 10,
                }}>
                  <span style={{ fontSize: 22 }}>{m.icon}</span>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600, display: "flex", justifyContent: "space-between" }}>
                      <span>{m.label}</span>
                      <span style={{ fontSize: 12 }}>
                        {cb ? <span style={{ color: "#f85149" }}>🔴 熔断</span>
                            : total > 0 ? <span style={{ color: "#12b76a" }}>🟢 {total} 个草稿</span>
                            : <span style={{ color: "var(--muted)" }}>—</span>}
                      </span>
                    </div>
                    {total > 0 && (
                      <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>
                        {Object.entries(breakdown).map(([k, v]) => `${statusLabelShort(k)}: ${v}`).join(" · ") || "—"}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* ========== 次级区：热点机会 + 复盘洞察 ========== */}
      <div className="grid two" style={{ marginTop: 16 }}>
        {/* 热点机会（审查报告 P1-07：改按钮文案从"转选题"→"查看"，只跳转不承诺动作） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">🔥 热点机会</div>
              <div className="panel-sub">来自 60s.viki.moe · R22 证据链接可追溯</div>
            </div>
            <a href="?page=hot" className="btn ghost">全部 →</a>
          </div>
          {hottopics.length === 0 ? (
            <EmptyHint next="到「热点雷达」扫描热点">暂无热点</EmptyHint>
          ) : (
            <div className="list">
              {hottopics.slice(0, 5).map((h, i) => {
                const ht = h as any;
                return (
                  <div key={i} className="task-row">
                    <i className="task-prio blue" />
                    <div className="task-main">
                      <div className="task-title" style={{ maxWidth: 420 }}>{ht.title ?? ht.name ?? JSON.stringify(h).slice(0, 40)}</div>
                      <div className="task-desc">
                        <span className="tag gray" style={{ fontSize: 10 }}>{ht.source ?? "—"}</span>
                        {ht.hotness != null && <span style={{ marginLeft: 8 }}>🔥 {ht.hotness}</span>}
                        {ht.evidence_status && ht.evidence_status !== "collected" && (
                          <span className="tag gray" style={{ marginLeft: 6, fontSize: 9 }}>{ht.evidence_status}</span>
                        )}
                      </div>
                    </div>
                    <a href={`?page=hot`} className="btn ghost" style={{ fontSize: 11 }}>→ 查看</a>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 复盘洞察与待决策（改"采纳"→"查看"，避免承诺未实现动作） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">💡 复盘洞察 · 待你决策</div>
              <div className="panel-sub">AI 自动策略调整建议 · 需人工采纳到母计划</div>
            </div>
            <a href="?page=analytics" className="btn ghost">全部 →</a>
          </div>
          {insights.length === 0 ? (
            <EmptyHint next="到「数据复盘」跑一轮分析">暂无复盘洞察</EmptyHint>
          ) : (
            <div className="list">
              {insights.slice(0, 4).map((it: any, i: number) => (
                <div key={i} className="task-row">
                  <i className="task-prio orange" />
                  <div className="task-main">
                    <div className="task-title">{it.type ?? "建议"}：{it.description ?? JSON.stringify(it).slice(0, 60)}</div>
                  </div>
                  <a href={`?page=analytics`} className="btn ghost" style={{ fontSize: 11 }}>→ 查看</a>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ========== 底部：阶段目标（按指标分组，不显示假总%） + 最近轮次 ========== */}
      <div className="grid two" style={{ marginTop: 16 }}>
        {/* 阶段目标：按 target_metric 分组（审查报告 P0-02 整改：不同指标不能加在一起） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">🎯 阶段目标（按指标分组）</div>
              <div className="panel-sub">不同指标不可直接相加 · R21 口径：未知 ≠ 0</div>
            </div>
            <a href="?page=goals" className="btn ghost">管理 →</a>
          </div>
          {activeGoals.length === 0 ? (
            <EmptyHint next="到「目标与策略」创建第一个阶段目标">暂无活跃目标</EmptyHint>
          ) : (
            <div style={{ display: "grid", gap: 12 }}>
              {Object.entries(goalsByMetric).map(([metric, gs]) => (
                <div key={metric} style={{ padding: 10, borderRadius: 8, border: "1px solid var(--border)", background: "var(--panel-alt)" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: "var(--accent)" }}>
                      📏 {metric} · {gs.length} 项目标
                    </div>
                    <div style={{ fontSize: 10, color: "var(--muted)" }}>按指标独立计算，不合并</div>
                  </div>
                  <div className="list" style={{ padding: 0 }}>
                    {gs.map((g: any) => {
                      const plat = (g.target_platform as Platform) ?? null;
                      const m = plat ? platformMeta(plat) : null;
                      return (
                        <div key={g.id} className="task-row" style={{ padding: "4px 0" }}>
                          <i className={`task-prio ${m ? "" : "blue"}`} style={{ background: m?.color }} />
                          <div className="task-main">
                            <div className="task-title" style={{ fontSize: 13 }}>{g.goal_text}</div>
                            <div className="task-desc" style={{ fontSize: 11 }}>
                              {plat ? `${m?.icon} ${m?.label}` : "全平台"} · 目标 {g.target_value}
                              {g.metric_scope ? ` · ${g.metric_scope}` : ""}
                            </div>
                          </div>
                          <div style={{ minWidth: 70, textAlign: "right" }}>
                            {g.achievement_rate != null ? (
                              <div style={{ fontSize: 13, fontWeight: 600, color: g.achievement_rate >= 100 ? "#12b76a" : "inherit" }}>
                                {g.achievement_rate.toFixed(0)}%
                              </div>
                            ) : (
                              <span className="tag gray" style={{ fontSize: 10 }}>待采集</span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 最近轮次 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">📋 最近轮次</div>
              <div className="panel-sub">螺旋引擎 9 态 · 创建时间倒序</div>
            </div>
            <a href="?page=production" className="btn ghost">全部 →</a>
          </div>
          {rounds.length === 0 ? (
            <EmptyHint next="到「内容生产」创建第一轮">暂无轮次</EmptyHint>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>轮次</th><th>状态</th><th>创建时间</th></tr>
                </thead>
                <tbody>
                  {rounds.slice(0, 6).map((r) => (
                    <tr key={r.id}>
                      <td>Round {r.round_number}</td>
                      <td><span className={`tag ${statusTag(r.status)}`}>{statusLabel(r.status)}</span></td>
                      <td style={{ color: "var(--muted)", fontSize: 12 }}>
                        {new Date(r.created_at).toLocaleString("zh-CN")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </AsyncPanel>
  );
}

// ============================================
// 子组件 + helper（全部留在本文件，不再分裂）
// ============================================

function KpiCard({
  icon, label, value, hint, tone, href,
}: {
  icon: string; label: string; value: string | number; hint?: string;
  tone?: "green" | "red" | "blue" | "orange" | "gray"; href?: string;
}) {
  const colorMap: Record<string, string> = {
    green: "#12b76a", red: "#f85149", blue: "#1f6bff", orange: "#d97706", gray: "var(--text)",
  };
  const card = (
    <div style={{
      background: "var(--panel)", border: "1px solid var(--border)",
      borderRadius: 10, padding: "14px 16px",
      ...(href ? { cursor: "pointer" } : {}),
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 11, color: "var(--muted)", letterSpacing: 0.5 }}>{icon} {label}</div>
      </div>
      <div style={{ fontSize: 28, fontWeight: 750, color: tone ? colorMap[tone] : "var(--text)", marginTop: 4 }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 2, lineHeight: 1.4 }}>{hint}</div>}
    </div>
  );
  return href ? <a href={href} style={{ textDecoration: "none", color: "inherit", display: "block" }}>{card}</a> : card;
}

function statusTag(s: string): string {
  const cls: Record<string, string> = {
    created: "gray", producing: "orange", awaiting_publish: "blue",
    collecting_metrics: "purple", analyzing: "teal",
    completed: "green", partial_completed: "orange", failed: "red", cancelled: "gray",
  };
  return cls[s] ?? "gray";
}
function statusLabel(s: string): string {
  const map: Record<string, string> = {
    created: "创建", producing: "生产中", awaiting_publish: "待发布",
    collecting_metrics: "采集中", analyzing: "复盘中",
    completed: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已取消",
  };
  return map[s] ?? s;
}
function statusLabelShort(s: string): string {
  const m: Record<string, string> = {
    draft_saved: "已存草稿", user_published: "已发布", data_fetching: "采集中",
    published: "已采集", failed: "失败", cancelled: "取消", expired: "过期",
    draft_pending: "待存草稿",
  };
  return m[s] ?? s;
}
function severityToTone(sev?: string): string {
  const m: Record<string, string> = { high: "red", medium: "orange", low: "blue" };
  return m[(sev ?? "medium").toLowerCase()] ?? "gray";
}
function actionItemTitle(it: any): string {
  if (it.type === "round_timeout") return `⏳ 轮次 Round ${it.round_id ?? "?"} 超过 72h 未确认发布`;
  if (it.type === "goal_overdue") return `🎯 目标 ${it.goal_text ?? ""} 已超期未达成`;
  if (it.type === "circuit_breaker_cooled") return `🔴 ${it.platform ?? ""} 熔断冷却到期，待恢复验证`;
  if (it.type === "circuit_breaker_active") return `🔴 ${it.platform ?? ""} 熔断中`;
  return it.reason ?? "待处理事项";
}
