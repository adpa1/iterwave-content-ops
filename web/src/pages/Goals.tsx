import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import { api, Goal, SystemConfigView } from "../api";

const METRICS = [
  { value: "followers_gained", label: "新增粉丝" },
  { value: "likes", label: "点赞数" },
  { value: "collects", label: "收藏数" },
  { value: "views", label: "曝光数" },
  { value: "comments", label: "评论数" },
  { value: "shares", label: "分享数" },
  { value: "interaction_rate", label: "互动率" },
];

const DATA_SOURCES = [
  { value: "account_stats", label: "account_stats 平台读回" },
  { value: "platform_api", label: "平台 API" },
  { value: "manual", label: "人工录入" },
];

function todayStr(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

const EMPTY_GOAL = {
  goal_text: "",
  target_metric: "followers_gained",
  target_value: 500,
  target_platform: "",
  metric_scope: "delta",
  baseline_value: 0,
  baseline_at: todayStr(),
  metric_period_start: todayStr(),
  metric_period_end: todayStr(30),
  data_source: "account_stats",
};

export default function Goals() {
  const { showToast, withPending } = useApp();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [cfgView, setCfgView] = useState<SystemConfigView | null>(null);
  const [newGoal, setNewGoal] = useState({ ...EMPTY_GOAL });
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setGoals(await api.goals());
      setCfgView(await api.config());
      setError(null);
    } catch (e: any) { setError(e.message); }
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async () => {
    if (!newGoal.goal_text.trim()) { showToast("请填写目标描述", "error"); return; }
    setSubmitting(true);
    try {
      // G-11：withPending 同步去重，连点 3 次只会发 1 个请求
      const res = await withPending("goal:create", async () =>
        api.createGoal({
          goal_text: newGoal.goal_text,
          target_metric: newGoal.target_metric,
          target_value: newGoal.target_value,
          target_platform: (newGoal.target_platform || null) as any,
          metric_scope: newGoal.metric_scope,
          baseline_value: newGoal.baseline_value,
          baseline_at: newGoal.baseline_at,
          metric_period_start: newGoal.metric_period_start,
          metric_period_end: newGoal.metric_period_end,
          data_source: newGoal.data_source,
        }),
      );
      if (res !== undefined) {
        showToast("✅ 目标已创建");
        await load();
        setNewGoal({ ...EMPTY_GOAL });
      }
    } catch (e: any) { showToast(`创建失败: ${e.message}`, "error"); }
    finally { setSubmitting(false); }
  };

  const handleRefreshProgress = async () => {
    setRefreshing(true);
    try {
      const res = await withPending("goal:refresh", () => api.refreshGoalProgress());
      if (res !== undefined) {
        setGoals(res.goals);
        showToast(`🔄 已刷新 ${Object.keys(res.updated).length} 个目标进度`);
      }
    } catch (e: any) { showToast(`刷新失败: ${e.message}`, "error"); }
    finally { setRefreshing(false); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">NORTH STAR & PLATFORM OBJECTIVES</div>
          <h1 className="page-title">目标与策略</h1>
          <div className="page-desc">
            总体目标定义方向；平台阶段目标定义每个平台当前要取得的结果。跨平台指标独立计算，不能简单相加。
          </div>
        </div>
        <div className="head-actions">
          <span className="tag gray">R21 口径定义：未知 ≠ 0</span>
          <button className="btn" onClick={handleRefreshProgress} disabled={refreshing}>
            {refreshing ? "刷新中…" : "↻ 刷新进度"}
          </button>
        </div>
      </div>

      {/* 原型 goals view：总体目标画像（账号北极星） */}
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◎</span>总体目标画像 · 账号北极星</div>
            <div className="panel-sub">R2：UltimateGoalChecker 的检查依据；文案/策略/评论 三场景均不得偏离此画像</div>
          </div>
        </div>
        {!cfgView ? (
          <div className="note">加载画像中…</div>
        ) : (
          <div className="grid two">
            <div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>账号人设关键词</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {cfgView.readonly.persona_keywords.length === 0 ? (
                  <span className="tag gray">未配置</span>
                ) : cfgView.readonly.persona_keywords.map((k) => (
                  <span key={k} className="tag blue">{k}</span>
                ))}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>禁止方向 / 红线关键词（{cfgView.readonly.redline_keywords_count} 个）</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {cfgView.readonly.forbidden_directions.length === 0 ? (
                  <span className="tag gray">未配置</span>
                ) : cfgView.readonly.forbidden_directions.map((k) => (
                  <span key={k} className="tag red">{k}</span>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ====== 新增：分项目标 + 策略约束 ====== */}
      {goals.length > 0 && cfgView && (
        <div className="grid two" style={{ marginTop: 16 }}>
          {/* 分项目标进度卡（按平台聚合） */}
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title">🎯 分项目标进度卡</div>
                <div className="panel-sub">总体目标拆解到各平台 · 自动聚合达成率</div>
              </div>
            </div>
            <div className="grid two">
              {["xhs", "douyin", "kuaishou", "wechat_channels"].map((p) => {
                const pGoals = goals.filter((g) => !g.target_platform || g.target_platform === p);
                if (pGoals.length === 0) return null;
                const avgRate =
                  pGoals.map((g) => g.achievement_rate).filter((r) => r != null);
                const rate = avgRate.length
                  ? avgRate.reduce((a, b) => a + b, 0) / avgRate.length
                  : null;
                const PLATFORM_META: Record<string, { label: string; emoji: string }> = {
                  xhs: { label: "小红书", emoji: "📕" },
                  douyin: { label: "抖音", emoji: "🎵" },
                  kuaishou: { label: "快手", emoji: "⚡" },
                  wechat_channels: { label: "视频号", emoji: "💚" },
                };
                const meta = PLATFORM_META[p]!;
                return (
                  <div
                    key={p}
                    style={{
                      padding: 12,
                      borderRadius: 8,
                      border: "1px solid var(--border)",
                      background: "var(--card)",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        marginBottom: 8,
                      }}
                    >
                      <span style={{ fontWeight: 700 }}>
                        {meta.emoji} {meta.label}
                      </span>
                      <span
                        style={{
                          fontSize: 18,
                          fontWeight: 800,
                          color:
                            rate == null
                              ? "var(--subtle)"
                              : rate >= 100
                              ? "var(--green)"
                              : rate >= 50
                              ? "var(--orange)"
                              : "var(--red)",
                        }}
                      >
                        {rate == null ? "—" : `${rate.toFixed(0)}%`}
                      </span>
                    </div>
                    <div
                      style={{
                        height: 8,
                        background: "var(--line)",
                        borderRadius: 4,
                        overflow: "hidden",
                      }}
                    >
                      <div
                        style={{
                          width: `${rate ?? 0}%`,
                          height: "100%",
                          background:
                            rate == null
                              ? "var(--line)"
                              : rate >= 100
                              ? "var(--green)"
                              : "var(--blue)",
                          transition: "width .3s",
                        }}
                      />
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--subtle)",
                        marginTop: 6,
                      }}
                    >
                      {pGoals.length} 项目标
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* 策略约束表 */}
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title">📋 策略与约束</div>
                <div className="panel-sub">R2/R22 红线 · 定位关键词 · 禁用方向</div>
              </div>
            </div>
            <div className="list">
              <div className="task-row">
                <i className="task-prio green" />
                <div className="task-main">
                  <div className="task-title">账号定位关键词</div>
                  <div className="task-desc" style={{ fontSize: 11 }}>
                    {(cfgView?.readonly.persona_keywords || []).join("、") || "—"}
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio orange" />
                <div className="task-main">
                  <div className="task-title">禁区方向</div>
                  <div className="task-desc" style={{ fontSize: 11 }}>
                    {(cfgView?.readonly.forbidden_directions || []).join("、") || "—"}
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio red" />
                <div className="task-main">
                  <div className="task-title">🔴 红线敏感词库（R22 强制开启，不可关闭）</div>
                  <div className="task-desc" style={{ fontSize: 11 }}>
                    已内置 {cfgView?.readonly.redline_keywords_count ?? 0} 条
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio blue" />
                <div className="task-main">
                  <div className="task-title">评分模式</div>
                  <div className="task-desc" style={{ fontSize: 11 }}>
                    由样本量自动切换：冷启动 → 样本积累 → 稳定运行
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio gray" />
                <div className="task-main">
                  <div className="task-title">LLM 模型</div>
                  <div className="task-desc" style={{ fontSize: 11 }}>
                    {cfgView?.readonly.llm_model || "—"} · API Key{" "}
                    {cfgView?.readonly.llm_has_api_key ? "✅ 已配" : "❌ 未配"}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">📌 新建阶段目标</div>
            <div className="panel-sub">R21：指标 / 目标值 / 口径 / 基线 / 统计周期 / 数据来源 缺一不可（缺则后端 422）</div>
          </div>
        </div>
        <div className="grid four" style={{ gap: 10 }}>
          <div>
            <label className="form-label">目标描述 *</label>
            <input className="field" placeholder="如：小红书涨粉 500"
              value={newGoal.goal_text} onChange={(e) => setNewGoal({ ...newGoal, goal_text: e.target.value })} />
          </div>
          <div>
            <label className="form-label">指标 *</label>
            <select className="field" value={newGoal.target_metric} onChange={(e) => setNewGoal({ ...newGoal, target_metric: e.target.value })}>
              {METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </div>
          <div>
            <label className="form-label">目标值 *</label>
            <input className="field" type="number" min={1} value={newGoal.target_value}
              onChange={(e) => setNewGoal({ ...newGoal, target_value: Number(e.target.value) })} />
          </div>
          <div>
            <label className="form-label">口径 *</label>
            <select className="field" value={newGoal.metric_scope} onChange={(e) => setNewGoal({ ...newGoal, metric_scope: e.target.value })}>
              <option value="delta">增量（本阶段新增）</option>
              <option value="total">总量（累计达到）</option>
            </select>
          </div>
          <div>
            <label className="form-label">基线值 *</label>
            <input className="field" type="number" value={newGoal.baseline_value}
              onChange={(e) => setNewGoal({ ...newGoal, baseline_value: Number(e.target.value) })} />
          </div>
          <div>
            <label className="form-label">基线时间 *</label>
            <input className="field" type="date" value={newGoal.baseline_at}
              onChange={(e) => setNewGoal({ ...newGoal, baseline_at: e.target.value })} />
          </div>
          <div>
            <label className="form-label">统计周期开始 *</label>
            <input className="field" type="date" value={newGoal.metric_period_start}
              onChange={(e) => setNewGoal({ ...newGoal, metric_period_start: e.target.value })} />
          </div>
          <div>
            <label className="form-label">统计周期结束 *</label>
            <input className="field" type="date" value={newGoal.metric_period_end}
              onChange={(e) => setNewGoal({ ...newGoal, metric_period_end: e.target.value })} />
          </div>
          <div>
            <label className="form-label">数据来源 *</label>
            <select className="field" value={newGoal.data_source} onChange={(e) => setNewGoal({ ...newGoal, data_source: e.target.value })}>
              {DATA_SOURCES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
            </select>
          </div>
          <div>
            <label className="form-label">平台（空=全平台）</label>
            <select className="field" value={newGoal.target_platform} onChange={(e) => setNewGoal({ ...newGoal, target_platform: e.target.value })}>
              <option value="">全平台</option>
              <option value="xhs">小红书</option>
              <option value="douyin">抖音</option>
              <option value="kuaishou">快手</option>
              <option value="wechat_channels">视频号</option>
            </select>
          </div>
        </div>
        <button className="btn primary" style={{ marginTop: 12 }} onClick={handleCreate} disabled={submitting}>
          {submitting ? "提交中…" : "创建目标"}
        </button>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◎</span>所有目标</div>
            <div className="panel-sub">达成率 =（当前值 − 基线）/ 目标值；任一分量缺失即显示「数据待采集」（不显示 0%）</div>
          </div>
        </div>
        {error && (
          <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
            <span>⚠️ {error}</span><button className="btn sm" onClick={load}>重试</button>
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>目标</th><th>平台</th><th>指标</th><th>目标值</th>
                <th>当前值</th><th>口径</th><th>周期</th><th>达成率</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              {goals.length === 0 && (
                <tr><td colSpan={9}>
                  <EmptyHint className="" next="用上方「新建阶段目标」表单创建第一个阶段目标">暂无目标</EmptyHint>
                </td></tr>
              )}
              {goals.map((g) => {
                const rate = g.achievement_rate;
                const progress = rate == null ? null : Math.max(0, Math.min(100, rate));
                return (
                  <tr key={g.id}>
                    <td style={{ fontWeight: 600 }}>{g.goal_text}</td>
                    <td>{g.target_platform || "全平台"}</td>
                    <td style={{ fontSize: 11 }}>{METRICS.find((m) => m.value === g.target_metric)?.label ?? g.target_metric}</td>
                    <td>{g.target_value}</td>
                    <td>{g.current_value != null
                      ? g.current_value
                      : <span style={{ color: "var(--subtle)" }}>数据待采集</span>}</td>
                    <td><span className="tag gray">{g.metric_scope === "delta" ? "增量" : "总量"}</span></td>
                    <td style={{ fontSize: 11, color: "var(--muted)" }}>
                      {g.metric_period_start ?? "—"} ~ {g.metric_period_end ?? "—"}
                    </td>
                    <td>
                      {progress != null ? (
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <div style={{ flex: 1, height: 6, background: "var(--line)", borderRadius: 3, minWidth: 60 }}>
                            <div style={{ width: `${progress}%`, height: "100%", background: "var(--blue)", borderRadius: 3 }} />
                          </div>
                          <span style={{ fontSize: 11, fontWeight: 600 }}>{progress.toFixed(0)}%</span>
                        </div>
                      ) : <span style={{ color: "var(--subtle)", fontSize: 11 }}>数据待采集</span>}
                    </td>
                    <td><span className={`tag ${g.status === "active" ? "green" : g.status === "completed" ? "blue" : "gray"}`}>{g.status}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
