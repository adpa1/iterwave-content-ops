import { Goal, GoalSummary } from "../api";
import { platformMeta } from "../platforms";

/**
 * 深色目标叙事卡（对齐 v10 demo 的 .goal-hero）。
 *
 * 全部数据来自 /api/goals、/api/goals/summary 与 /api/config，不含任何硬编码数值：
 * - 总进度 / 周期 / 剩余天数来自 /api/goals/summary（后端 GoalProgressUpdater.summarize，
 *   已有数据的活跃目标达成率等权平均；没有数据 → null，显示「数据待采集」，R21：未知 ≠ 0）。
 * - 右侧关键结果 = 活跃目标（按达成率缺失优先暴露、再按截止时间）取前 3 个。
 */

export const METRIC_LABELS: Record<string, string> = {
  followers_gained: "新增粉丝",
  new_followers: "新增粉丝",
  like_rate: "点赞率",
  collect_rate: "收藏率",
  published_count: "发布篇数",
  likes: "点赞数",
  collects: "收藏数",
  views: "曝光数",
  comments: "评论数",
  shares: "分享数",
  interaction_rate: "互动率",
};

export const PERCENT_METRICS = new Set(["interaction_rate", "like_rate", "collect_rate"]);

export function metricLabel(m: string) {
  return METRIC_LABELS[m] ?? m;
}

export function isActiveGoal(g: Goal) {
  return !g.status || g.status === "active";
}

export function formatMetricValue(metric: string, v: number | null | undefined): string {
  if (v == null) return "—";
  // 比率类指标后端统一以百分数存储（goal_progress.PERCENT_METRICS）：3.8 即 3.8%
  if (PERCENT_METRICS.has(metric)) return `${Number(v.toFixed(2))}%`;
  return v.toLocaleString("zh-CN");
}

/** delta 口径显示「当前值 − 基线」，与达成率公式一致；absolute 口径显示当前值本身 */
export function currentProgressText(g: Goal): string {
  if (g.current_value == null) return "待采集";
  if (g.metric_scope === "delta" && g.baseline_value != null) {
    const d = g.current_value - g.baseline_value;
    return `${d >= 0 ? "+" : ""}${formatMetricValue(g.target_metric, d)}`;
  }
  return formatMetricValue(g.target_metric, g.current_value);
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  const d = new Date(s);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

interface Props {
  goals: Goal[];
  summary: GoalSummary | null;
  personaKeywords?: string[];
  forbiddenDirections?: string[];
  onCreateGoal?: () => void;
}

export default function GoalHero({ goals, summary, personaKeywords = [], forbiddenDirections = [], onCreateGoal }: Props) {
  const active = goals.filter(isActiveGoal);
  const overall = {
    percent: summary?.percent ?? null,
    counted: summary?.counted_goals ?? 0,
    pending: summary?.pending_goals ?? 0,
    start: summary?.period_start ?? null,
    end: summary?.period_end ?? null,
    daysLeft: summary?.days_left ?? null,
  };
  const krs = [...active]
    .sort((a, b) => (a.metric_period_end ?? "").localeCompare(b.metric_period_end ?? ""))
    .slice(0, 3);

  const title = personaKeywords.length
    ? `账号定位：${personaKeywords.join(" · ")}`
    : "尚未配置账号定位关键词";

  return (
    <section className="goal-hero" aria-label="总体目标">
      <div style={{ position: "relative", zIndex: 1 }}>
        <div className="goal-label">
          总体目标 · 账号北极星
          {overall.start && overall.end ? ` · ${fmtDate(overall.start)} – ${fmtDate(overall.end)}` : ""}
        </div>
        <div className="goal-title">{title}</div>
        <div className="goal-copy">
          {active.length
            ? `${active.length} 个活跃阶段目标，各平台指标独立计算、不直接相加；总进度只统计已有数据的目标。`
            : "还没有活跃的阶段目标。先为平台设定可衡量的阶段目标，总进度才有计算依据。"}
          {forbiddenDirections.length > 0 && ` 禁止方向：${forbiddenDirections.join("、")}。`}
        </div>
        <div className="flex wrap" style={{ marginTop: 14 }}>
          <span className="tag" style={{ background: "#ffffff1b", color: "#dbe5ff" }}>
            {overall.percent != null ? `目标进度 ${Math.round(overall.percent)}%` : "目标进度 · 数据待采集"}
          </span>
          <span style={{ fontSize: 10, color: "#b4c8e8" }}>
            {overall.percent != null
              ? `按 ${overall.counted} 个有数据目标的达成率等权平均${overall.pending ? `，${overall.pending} 个待采集` : ""}`
              : active.length ? `${overall.pending} 个目标的当前值尚未采集` : ""}
            {overall.daysLeft != null && (overall.daysLeft >= 0 ? ` · 距阶段结束 ${overall.daysLeft} 天` : " · 阶段已结束")}
          </span>
        </div>
        <div
          className="bar-light"
          role="progressbar"
          aria-label="总体目标进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={overall.percent != null ? Math.round(overall.percent) : undefined}
        >
          <span style={{ width: `${overall.percent ?? 0}%` }} />
        </div>
      </div>

      <div className="goal-right">
        {krs.length === 0 ? (
          <div className="goal-stat" style={{ gridColumn: "1 / -1" }}>
            <div className="l">关键结果</div>
            <div className="n" style={{ fontSize: 14 }}>暂无阶段目标</div>
            {onCreateGoal && (
              <button type="button" className="btn sm" style={{ marginTop: 8 }} onClick={onCreateGoal}>
                去创建目标 →
              </button>
            )}
          </div>
        ) : (
          krs.map((g) => {
            const plat = g.target_platform ? platformMeta(g.target_platform).label : "全平台";
            return (
              <div key={g.id} className="goal-stat" title={g.goal_text}>
                <div className="l">{plat} · {metricLabel(g.target_metric)}</div>
                <div className="n">
                  {g.metric_scope === "delta" && !PERCENT_METRICS.has(g.target_metric) ? "+" : ""}
                  {formatMetricValue(g.target_metric, g.target_value)}
                </div>
                <div className="sub" style={g.current_value == null ? { color: "#c9d6ea" } : undefined}>
                  {g.current_value == null
                    ? "当前值待采集"
                    : `当前 ${currentProgressText(g)}${g.achievement_rate != null ? ` · ${Math.round(g.achievement_rate)}%` : ""}`}
                </div>
              </div>
            );
          })
        )}
        {krs.length > 0 && krs.length < 3 && onCreateGoal && (
          <button type="button" className="goal-stat goal-stat-add" onClick={onCreateGoal}>
            <div className="l">关键结果</div>
            <div className="n" style={{ fontSize: 14 }}>＋ 添加阶段目标</div>
            <div className="sub">建议每个启用平台设一个</div>
          </button>
        )}
      </div>
    </section>
  );
}
