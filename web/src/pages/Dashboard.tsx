import React, { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import GoalHero, { isActiveGoal, metricLabel, currentProgressText } from "../components/GoalHero";
import {
  api, SpiralRound, Goal, HotTopicItem, InsightItem, PlatformProgress, ActionItem, SystemConfigView,
} from "../api";
import { platformMeta, Platform, ALL_PLATFORMS } from "../platforms";

/**
 * 运营总览 —— 版式对齐 v10 demo（goal-hero → KPI → 决策/热点 → 四平台 lane → Agent 编排/目标）。
 * 所有数字都来自后端接口：
 *   /api/status /api/rounds /api/goals /api/hot_topics /api/insights
 *   /api/platform_progress /api/action_items /api/config
 * 任何接口没有数据时显示空态或「待采集」，不填演示数。
 */

const PLATFORM_CLS: Record<Platform, string> = {
  xhs: "xhs", douyin: "douyin", kuaishou: "kuaishou", wechat_channels: "channels",
};

/** 单篇草稿在后端的状态流转顺序（lane 阶段条按此渲染） */
const DRAFT_STAGES: { key: string; label: string }[] = [
  { key: "draft_pending", label: "待存草稿" },
  { key: "draft_saved", label: "已存草稿" },
  { key: "user_published", label: "已发布" },
  { key: "data_fetching", label: "数据采集" },
  { key: "published", label: "已采集" },
];

const ROUND_STATUS_LABEL: Record<string, string> = {
  created: "已创建", producing: "生产中", awaiting_publish: "待发布",
  collecting_metrics: "采集中", analyzing: "复盘中",
  completed: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已取消",
};

export default function Dashboard() {
  const { status } = useApp();
  const [rounds, setRounds] = useState<SpiralRound[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [hot, setHot] = useState<HotTopicItem[]>([]);
  const [insights, setInsights] = useState<InsightItem[]>([]);
  const [progress, setProgress] = useState<Partial<Record<Platform, PlatformProgress>>>({});
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [cfg, setCfg] = useState<SystemConfigView | null>(null);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    const r = await Promise.allSettled([
      api.rounds(),
      api.goals(),
      api.hotTopics(),
      api.insights(),
      api.platformProgress(),
      api.actionItems(),
      api.config(),
    ]);
    if (r[0].status === "fulfilled") setRounds(r[0].value);
    if (r[1].status === "fulfilled") setGoals(r[1].value);
    if (r[2].status === "fulfilled") setHot(r[2].value.items ?? []);
    if (r[3].status === "fulfilled") setInsights(r[3].value.items ?? []);
    if (r[4].status === "fulfilled") setProgress(r[4].value ?? {});
    if (r[5].status === "fulfilled") setActions(r[5].value.items ?? []);
    if (r[6].status === "fulfilled") setCfg(r[6].value);
    const failed = r.filter((x) => x.status === "rejected").length;
    if (failed === r.length) setError("后端接口不可用，请确认 API 服务已启动");
    else if (failed) setError(null);
    setLoadedAt(new Date());
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const go = (page: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set("page", page);
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  // ===== 派生（全部来自接口） =====
  const enabled = (status?.enabled_platforms ?? []) as Platform[];
  const loggedIn = (status?.logged_in_platforms ?? []) as Platform[];
  const broken = (status?.circuit_broken_platforms ?? []) as Platform[];

  const awaitingRounds = rounds.filter((r) => r.status === "awaiting_publish");
  const runningRounds = rounds.filter((r) => ["created", "producing", "awaiting_publish"].includes(r.status));
  // 「机会」= 已通过维度匹配（账号定位）且可发布；dimension_match 为 null 表示尚未研判，不能算进机会
  const publishableHot = hot.filter((h) => h.publishable && h.dimension_match === true);
  const unjudgedHot = hot.filter((h) => h.dimension_match == null);
  const shortWindowHot = publishableHot.filter((h) => (h.time_sensitive ?? 0) > 0);
  const weekAgo = Date.now() - 7 * 86400000;
  const insightsThisWeek = insights.filter((i) => new Date(i.analyzed_at).getTime() >= weekAgo);
  const insightsWithSamples = insightsThisWeek.filter((i) => (i.sample_count ?? 0) > 0);
  const highActions = actions.filter((a) => a.priority === "high");
  const totalDrafts = Object.values(progress).reduce((s, p) => s + (p?.total_drafts ?? 0), 0);
  const latestRound = rounds[0];

  return (
    <AsyncPanel loading={loading} error={error} onRetry={load} loadingText="加载运营总览…">
      <div className="page-head">
        <div>
          <div className="eyebrow">Command Center</div>
          <h1 className="page-title">运营总览</h1>
          <div className="page-desc">
            一个总体目标、各平台独立执行；Agent 负责发现机会、编排任务、汇总进展，关键决策由你确认。
            {loadedAt && <span className="tiny" style={{ marginLeft: 8 }}>数据更新于 {loadedAt.toLocaleTimeString("zh-CN")}</span>}
          </div>
        </div>
        <div className="head-actions">
          <button type="button" className="btn" onClick={load}>↻ 刷新</button>
          <button type="button" className="btn" onClick={() => go("agent")}>查看 Agent 运行</button>
          <button type="button" className="btn primary" onClick={() => go("hot")}>✳ 扫描热点</button>
        </div>
      </div>

      <GoalHero
        goals={goals}
        personaKeywords={cfg?.readonly.persona_keywords}
        forbiddenDirections={cfg?.readonly.forbidden_directions}
        onCreateGoal={() => go("goals")}
      />

      {/* ===== KPI ===== */}
      <div className="grid kpis" style={{ marginBottom: 15 }}>
        <Metric
          label="待你处理"
          icon="✋"
          value={actions.length + awaitingRounds.length}
          unit="项"
          foot={[
            highActions.length ? <span key="h" style={{ color: "var(--red)", fontWeight: 700 }}>{highActions.length} 项高优先级</span> : <span key="h">无高优先级</span>,
            <span key="a">{awaitingRounds.length} 轮待确认发布</span>,
          ]}
        />
        <Metric
          label="进行中的轮次"
          icon="⎇"
          value={runningRounds.length}
          unit="轮"
          foot={[<span key="d">{totalDrafts} 篇草稿</span>, <span key="p">{enabled.length} 个平台启用</span>]}
        />
        <Metric
          label="热点机会池"
          icon="✳"
          iconTone="orange"
          value={publishableHot.length}
          unit="条匹配定位"
          foot={[
            shortWindowHot.length ? <span key="s" style={{ color: "var(--orange)", fontWeight: 700 }}>{shortWindowHot.length} 条时效敏感</span> : <span key="s">无时效敏感</span>,
            <span key="t">{unjudgedHot.length} 条待研判 · 共 {hot.length} 条</span>,
          ]}
        />
        <Metric
          label="本周复盘"
          icon="↗"
          iconTone="teal"
          value={insightsThisWeek.length}
          unit="个平台"
          foot={[<span key="w">{insightsWithSamples.length} 个有有效样本</span>, <span key="p">已发布样本 {status?.published_samples ?? 0}</span>]}
        />
      </div>

      {/* ===== 决策 + 热点 ===== */}
      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">✓</span>需要你决策的事项</div>
              <div className="panel-sub">来自 /api/action_items 与待发布轮次，按优先级排序</div>
            </div>
            <button type="button" className="btn sm" onClick={() => go("operations")}>全部处理 →</button>
          </div>
          {actions.length === 0 && awaitingRounds.length === 0 ? (
            <EmptyHint next="Agent 产出待确认内容或出现告警时会出现在这里">暂无需要你决策的事项</EmptyHint>
          ) : (
            <div className="list">
              {[...actions].sort(byPriority).slice(0, 6).map((it) => (
                <div key={it.id} className="task-row">
                  <i className={`task-prio ${priorityTone(it.priority)}`} aria-hidden="true" />
                  <div className="task-main">
                    <div className="task-title">{it.title}</div>
                    {it.detail && <div className="task-desc">{it.detail}</div>}
                  </div>
                  <div className="task-actions">
                    <button type="button" className="btn sm" onClick={() => go(actionTarget(it))}>处理</button>
                  </div>
                </div>
              ))}
              {awaitingRounds.slice(0, Math.max(0, 6 - actions.length)).map((r) => (
                <div key={`r${r.id}`} className="task-row">
                  <i className="task-prio" aria-hidden="true" />
                  <div className="task-main">
                    <div className="task-title">Round {r.round_number} 草稿待确认发布</div>
                    <div className="task-desc">创建于 {new Date(r.created_at).toLocaleString("zh-CN")}</div>
                  </div>
                  <div className="task-actions">
                    <button type="button" className="btn primary sm" onClick={() => go("production")}>审核</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">
                <span className="icon" style={{ background: "var(--orange-weak)", color: "var(--orange)" }} aria-hidden="true">✳</span>
                热点机会 · 与目标的关联
              </div>
              <div className="panel-sub">只列与账号定位匹配、且通过可发布性检查的热点</div>
            </div>
            <button type="button" className="btn sm" onClick={() => go("hot")}>热点雷达 →</button>
          </div>
          {publishableHot.length === 0 ? (
            <EmptyHint next={unjudgedHot.length ? "到「热点雷达」对已采集热点做维度研判" : "到「热点雷达」刷新采集"}>
              {hot.length
                ? `已采集 ${hot.length} 条，${unjudgedHot.length ? `${unjudgedHot.length} 条尚未研判，` : ""}暂无与账号定位匹配的热点`
                : "暂无热点数据"}
            </EmptyHint>
          ) : (
            <div className="list">
              {publishableHot.slice(0, 5).map((h) => (
                <div key={h.id} className="task-row">
                  <i className={`task-prio ${(h.time_sensitive ?? 0) > 0 ? "red" : "blue"}`} aria-hidden="true" />
                  <div className="task-main">
                    <div className="task-title">{h.title}</div>
                    <div className="task-desc">
                      <span className="source-chip">{h.source}</span>
                      {h.dimension ? ` 维度 ${h.dimension}` : ""}
                      {h.heat != null ? ` · 热度 ${h.heat.toLocaleString("zh-CN")}${h.heat_unit ?? ""}` : ""}
                      {(h.time_sensitive ?? 0) > 0 ? " · 时效敏感" : ""}
                    </div>
                  </div>
                  <div className="task-actions">
                    <button type="button" className="btn sm" onClick={() => go("hot")}>评估</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ===== 四平台 lane ===== */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon" aria-hidden="true">▦</span>四个平台独立进度</div>
            <div className="panel-sub">共用总体目标，但草稿进度、登录状态、阶段目标和复盘结论各自独立</div>
          </div>
          <button type="button" className="btn sm" onClick={() => go("production")}>进入内容生产 →</button>
        </div>
        <div className="platform-lanes">
          {ALL_PLATFORMS.map((p) => (
            <PlatformLane
              key={p}
              platform={p}
              enabled={enabled.includes(p)}
              loggedIn={loggedIn.includes(p)}
              broken={broken.includes(p) || !!progress[p]?.circuit_broken}
              progress={progress[p]}
              goal={goals.filter(isActiveGoal).find((g) => g.target_platform === p)}
              insight={insights.find((i) => i.platform === p)}
              onOpen={(page) => go(page)}
            />
          ))}
        </div>
      </div>

      {/* ===== Agent 编排 + 目标 ===== */}
      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">⎇</span>Agent 当前编排</div>
              <div className="panel-sub">
                {latestRound
                  ? `最近一轮：Round ${latestRound.round_number} · ${ROUND_STATUS_LABEL[latestRound.status] ?? latestRound.status}`
                  : "还没有运行过螺旋轮次"}
              </div>
            </div>
            <button type="button" className="btn sm" onClick={() => go("agent")}>执行详情 →</button>
          </div>
          <AgentFlow
            round={latestRound}
            hotCount={hot.length}
            publishableHot={publishableHot.length}
            unjudgedHot={unjudgedHot.length}
            drafts={totalDrafts}
            awaiting={awaitingRounds.length}
            insights={insightsWithSamples.length}
          />
          <div className="note blue" style={{ marginTop: 12 }}>
            人工决策门：热点切入角度、内容定稿、发布确认、策略调整。系统只保存草稿，不会自动点发布。
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">◎</span>平台阶段目标</div>
              <div className="panel-sub">各平台指标独立计算 · 未采集显示「待采集」而不是 0</div>
            </div>
            <button type="button" className="btn sm" onClick={() => go("goals")}>管理目标 →</button>
          </div>
          {goals.filter(isActiveGoal).length === 0 ? (
            <EmptyHint next="到「目标与策略」创建第一个阶段目标">暂无活跃目标</EmptyHint>
          ) : (
            <div className="list">
              {goals.filter(isActiveGoal).slice(0, 6).map((g) => (
                <div key={g.id} className="task-row">
                  <i className={`task-prio ${g.achievement_rate == null ? "" : g.achievement_rate >= 100 ? "green" : "blue"}`} aria-hidden="true" />
                  <div className="task-main">
                    <div className="task-title">{g.goal_text}</div>
                    <div className="task-desc">
                      {g.target_platform ? platformMeta(g.target_platform).label : "全平台"} · {metricLabel(g.target_metric)} · 当前 {currentProgressText(g)}
                    </div>
                    {g.achievement_rate != null && (
                      <div className="progress" style={{ marginTop: 6 }}>
                        <span style={{ width: `${Math.min(100, Math.max(0, g.achievement_rate))}%` }} />
                      </div>
                    )}
                  </div>
                  <span className={`tag ${g.achievement_rate == null ? "gray" : "blue"}`}>
                    {g.achievement_rate == null ? "待采集" : `${Math.round(g.achievement_rate)}%`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </AsyncPanel>
  );
}

// ============================================================
// 子组件
// ============================================================

function Metric({ label, icon, iconTone, value, unit, foot }: {
  label: string; icon: string; iconTone?: "orange" | "teal"; value: number; unit: string; foot: React.ReactNode[];
}) {
  const toneStyle = iconTone === "orange"
    ? { background: "var(--orange-weak)", color: "var(--orange)" }
    : iconTone === "teal" ? { background: "var(--teal-weak)", color: "var(--teal)" } : undefined;
  return (
    <div className="metric">
      <div className="metric-top">
        <span className="metric-label">{label}</span>
        <span className="metric-icon" style={toneStyle} aria-hidden="true">{icon}</span>
      </div>
      <div className="metric-val">
        {value} <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 500 }}>{unit}</span>
      </div>
      <div className="metric-foot">{foot}</div>
    </div>
  );
}

function PlatformLane({ platform, enabled, loggedIn, broken, progress, goal, insight, onOpen }: {
  platform: Platform; enabled: boolean; loggedIn: boolean; broken: boolean;
  progress?: PlatformProgress; goal?: Goal; insight?: InsightItem; onOpen: (page: string) => void;
}) {
  const m = platformMeta(platform);
  const breakdown = progress?.status_breakdown ?? {};
  const total = progress?.total_drafts ?? 0;
  // 当前阶段 = 最靠后的、有草稿的阶段
  let current = -1;
  DRAFT_STAGES.forEach((s, i) => { if ((breakdown[s.key] ?? 0) > 0) current = i; });

  const state = !enabled
    ? { cls: "gray", text: "未启用" }
    : broken ? { cls: "red", text: "熔断冷却" }
    : !loggedIn ? { cls: "orange", text: "未登录" }
    : total > 0 ? { cls: "green", text: "运行中" }
    : { cls: "blue", text: "待开始" };

  const failed = (breakdown.failed ?? 0) + (breakdown.expired ?? 0);
  const next = !enabled ? "在系统设置中启用该平台"
    : broken ? "到运营控制台执行熔断恢复验证"
    : !loggedIn ? "扫码登录后才能保存草稿"
    : (breakdown.draft_saved ?? 0) > 0 ? `${breakdown.draft_saved} 篇草稿待你确认发布`
    : total === 0 ? "等待新一轮生产"
    : "等待数据采集";

  return (
    <div className="lane" style={!enabled ? { opacity: 0.7 } : undefined}>
      <div className="lane-head">
        <div className="lane-name">
          <span className={`platform-logo ${PLATFORM_CLS[platform]}`} aria-hidden="true">{m.label.slice(0, 1)}</span>
          {m.label}
        </div>
        <span className={`tag ${state.cls}`}>{state.text}</span>
      </div>
      <div className="lane-meta">
        草稿 {total} 篇{failed ? ` · 失败/过期 ${failed}` : ""} · 目标：
        {goal ? `${metricLabel(goal.target_metric)} ${goal.achievement_rate != null ? `${Math.round(goal.achievement_rate)}%` : "待采集"}` : "未设置"}
      </div>
      <div className="lane-stages">
        {DRAFT_STAGES.map((s, i) => {
          const n = breakdown[s.key] ?? 0;
          const cls = current < 0 ? "" : i < current ? "done" : i === current ? (broken ? "issue" : "current") : "";
          return (
            <span key={s.key} className={`stage ${cls}`}>
              {s.label}{n ? ` ${n}` : ""}
            </span>
          );
        })}
      </div>
      {insight?.conclusion && (
        <div className="tiny" style={{ marginTop: 8 }} title={insight.conclusion}>复盘：{insight.conclusion}</div>
      )}
      <div className="lane-foot">
        <small>下一动作：{next}</small>
        <div className="flex">
          {enabled && !loggedIn && (
            <button type="button" className="btn sm soft" onClick={() => onOpen("accounts")}>去登录</button>
          )}
          <button type="button" className="btn sm" onClick={() => onOpen(broken ? "operations" : "production")}>查看任务</button>
        </div>
      </div>
    </div>
  );
}

function AgentFlow({ round, hotCount, publishableHot, unjudgedHot, drafts, awaiting, insights }: {
  round?: SpiralRound; hotCount: number; publishableHot: number; unjudgedHot: number; drafts: number; awaiting: number; insights: number;
}) {
  // 以最近一轮的状态推断当前所处步骤；没有轮次时停在「发现」
  const stepOf: Record<string, number> = {
    created: 1, producing: 2, awaiting_publish: 3, collecting_metrics: 4, analyzing: 4,
    completed: 5, partial_completed: 5,
  };
  const active = round ? (stepOf[round.status] ?? 0) : 0;
  const halted = round && ["failed", "cancelled"].includes(round.status);

  const steps = [
    { t: "发现", d: "热点、行业资讯采集", s: hotCount ? `采集 ${hotCount} 条` : "暂无采集" },
    { t: "研判", d: "匹配账号定位与红线", s: unjudgedHot ? `${publishableHot} 条匹配 · ${unjudgedHot} 待研判` : `${publishableHot} 条匹配` },
    { t: "策划与创作", d: "各平台独立生产与质量门", s: `${drafts} 篇草稿` },
    { t: "发布与运营", d: "存草稿，人工确认发布", s: awaiting ? `${awaiting} 轮待确认` : "无待确认" },
    { t: "归因复盘", d: "单平台复盘回写策略", s: insights ? `${insights} 个平台有结论` : "等待数据" },
  ];

  return (
    <div className="agent-flow">
      {steps.map((st, i) => {
        const cls = halted && i === active ? "pause" : i < active ? "done" : i === active ? "active" : "";
        return (
          <div key={st.t} className={`flow-step ${cls}`} aria-current={i === active ? "step" : undefined}>
            <div className="num">STEP 0{i + 1}</div>
            <div className="ft">{st.t}</div>
            <div className="fd">{st.d}</div>
            <div className="fs">{i < active ? "✓ " : i === active ? "● " : ""}{st.s}</div>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================
// helpers
// ============================================================

const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };
function byPriority(a: ActionItem, b: ActionItem) {
  return (PRIORITY_ORDER[a.priority ?? "medium"] ?? 1) - (PRIORITY_ORDER[b.priority ?? "medium"] ?? 1);
}
function priorityTone(p?: string) {
  return p === "high" ? "red" : p === "low" ? "blue" : "";
}
function actionTarget(it: ActionItem): string {
  if (it.type === "goal_strategy") return "goals";
  if (it.type === "publish_decision") return "production";
  return "operations";
}
