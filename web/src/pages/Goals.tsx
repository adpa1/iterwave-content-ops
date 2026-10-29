import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import { api, Goal, GoalSummary, SystemConfigView, type PersonasView } from "../api";
import PersonaMatrix, { GOAL_MODE_META, ROLE_LABELS, RELATION_LABELS } from "../components/PersonaMatrix";
import { useUrlTab } from "../nav";
import GoalHero, { PERCENT_METRICS, isActiveGoal, metricLabel, formatMetricValue, currentProgressText } from "../components/GoalHero";
import { ALL_PLATFORMS, Platform, platformMeta } from "../platforms";

const GOAL_STATUS_LABEL: Record<string, string> = {
  active: "进行中", completed: "已达成", expired: "已过期", cancelled: "已取消", paused: "已暂停",
};

const PLATFORM_CLS: Record<Platform, string> = {
  xhs: "xhs", douyin: "douyin", kuaishou: "kuaishou", wechat_channels: "channels",
};

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
  { value: "account_stats", label: "平台后台读回" },
  { value: "platform_api", label: "平台 API" },
  { value: "manual", label: "人工录入" },
];

/** 目标描述 → 平台识别：写「小红书涨粉 500」时自动选中小红书，避免落到「全平台」统计口径 */
const PLATFORM_KEYWORDS: { re: RegExp; platform: Platform }[] = [
  { re: /小红书|红书|xhs/i, platform: "xhs" },
  { re: /抖音|douyin/i, platform: "douyin" },
  { re: /快手|kuaishou/i, platform: "kuaishou" },
  { re: /视频号|微信视频号|channels/i, platform: "wechat_channels" },
];

function detectPlatform(text: string): Platform | null {
  return PLATFORM_KEYWORDS.find((k) => k.re.test(text))?.platform ?? null;
}

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
  goal_level: "platform",
  parent_goal_id: "",
  weight: "",
  contribution_note: "",
};

const GOAL_TABS = [
  { k: "progress", l: "目标进度" },
  { k: "persona", l: "账号定位与画像" },
  { k: "list", l: "全部目标" },
] as const;
type GoalTab = typeof GOAL_TABS[number]["k"];

export default function Goals() {
  const { showToast, withPending, status } = useApp();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [summary, setSummary] = useState<GoalSummary | null>(null);
  const [cfgView, setCfgView] = useState<SystemConfigView | null>(null);
  const [newGoal, setNewGoal] = useState({ ...EMPTY_GOAL });
  /** 用户是否手动选过平台：没选过时按目标描述自动识别 */
  const [platformTouched, setPlatformTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 编辑中的目标（null = 未编辑）；current_value 只由采集回写，这里不提供 */
  const [editing, setEditing] = useState<Goal | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [personas, setPersonas] = useState<PersonasView | null>(null);
  const [tab, setTab] = useUrlTab<GoalTab>(GOAL_TABS.map((t) => t.k), "progress");

  const load = async () => {
    try {
      const [gs, sm] = await Promise.all([api.goals(), api.goalsSummary()]);
      setGoals(gs);
      setSummary(sm);
      setCfgView(await api.config());
      api.personas().then(setPersonas).catch(() => setPersonas(null));
      setError(null);
    } catch (e: any) { setError(e.message); }
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async () => {
    if (!newGoal.goal_text.trim()) { showToast("请填写目标描述", "error"); return; }
    // 平台是统计口径的前提：不知道是哪个平台，「涨粉 500」就没法算进度
    if (newGoal.goal_level === "platform" && !newGoal.target_platform) {
      showToast("请选择目标平台（决定只统计哪个平台的数据）", "error");
      return;
    }
    setSubmitting(true);
    try {
      // G-11：withPending 同步去重，连点 3 次只会发 1 个请求
      const res = await withPending("goal:create", async () =>
        api.createGoal({
          goal_text: newGoal.goal_text,
          target_metric: newGoal.target_metric,
          target_value: newGoal.target_value,
          target_platform: newGoal.target_platform === "all" ? null : (newGoal.target_platform || null) as any,
          metric_scope: newGoal.metric_scope,
          baseline_value: newGoal.baseline_value,
          baseline_at: newGoal.baseline_at,
          metric_period_start: newGoal.metric_period_start,
          metric_period_end: newGoal.metric_period_end,
          data_source: newGoal.data_source,
          goal_level: newGoal.goal_level as Goal["goal_level"],
          parent_goal_id: newGoal.goal_level === "platform" && newGoal.parent_goal_id ? newGoal.parent_goal_id : null,
          weight: newGoal.weight === "" ? null : Number(newGoal.weight),
          contribution_note: newGoal.contribution_note.trim() || null,
        }),
      );
      if (res !== undefined) {
        showToast("✓ 目标已创建");
        await load();
        setNewGoal({ ...EMPTY_GOAL });
        setPlatformTouched(false);
        setCreating(false);
      }
    } catch (e: any) { showToast(`创建失败: ${e.message}`, "error"); }
    finally { setSubmitting(false); }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    const targetValue = Number(editing.target_value);
    if (!(targetValue > 0)) { showToast("目标值必须大于 0", "error"); return; }
    setSavingEdit(true);
    try {
      const res = await withPending("goal:update", () =>
        api.updateGoal(editing.id, {
          goal_text: editing.goal_text,
          target_metric: editing.target_metric,
          target_value: targetValue,
          target_platform: (editing.target_platform || null) as any,
          metric_scope: editing.metric_scope,
          baseline_value: editing.baseline_value == null ? null : Number(editing.baseline_value),
          baseline_at: editing.baseline_at,
          metric_period_start: editing.metric_period_start,
          metric_period_end: editing.metric_period_end,
          data_source: editing.data_source,
          status: editing.status,
        }),
      );
      if (res !== undefined) {
        showToast("✓ 目标已更新");
        setEditing(null);
        await load();
      }
    } catch (e: any) { showToast(`保存失败: ${e.message}`, "error"); }
    finally { setSavingEdit(false); }
  };

  const handleRefreshProgress = async () => {
    setRefreshing(true);
    try {
      const res = await withPending("goal:refresh", () => api.refreshGoalProgress());
      if (res !== undefined) {
        setGoals(res.goals);
        setSummary(res.summary);
        showToast(`↻ 已刷新 ${Object.keys(res.updated).length} 个目标进度`);
      }
    } catch (e: any) { showToast(`刷新失败: ${e.message}`, "error"); }
    finally { setRefreshing(false); }
  };

  const mode = personas?.goal_mode ?? summary?.mode ?? "unified";
  const northStars = goals.filter((g) => isActiveGoal(g) && g.goal_level === "north_star");
  const goalById = Object.fromEntries(goals.map((g) => [g.id, g]));

  const recordFollowers = async (p: Platform) => {
    const raw = window.prompt(`${platformMeta(p).label} 当前粉丝总数（涨粉目标按这个值计算进度）`);
    if (raw == null || raw.trim() === "") return;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) { showToast("请输入非负数字", "error"); return; }
    try {
      await api.addAccountSnapshot({ platform: p, followers: Math.round(n) });
      showToast(`✓ 已记录 ${platformMeta(p).label} 粉丝数 ${Math.round(n)}，目标进度已刷新`, "success");
      await load();
    } catch (e: any) { showToast(`记录失败：${e?.message ?? e}`, "error"); }
  };

  const fillManualValue = async (g: Goal) => {
    const raw = window.prompt(`「${g.goal_text}」当前值`, g.current_value != null ? String(g.current_value) : "");
    if (raw == null || raw.trim() === "") return;
    const n = Number(raw);
    if (!Number.isFinite(n)) { showToast("请输入数字", "error"); return; }
    try {
      await api.setGoalManualValue(g.id, n);
      showToast("✓ 当前值已更新", "success");
      await load();
    } catch (e: any) { showToast(`更新失败：${e?.message ?? e}`, "error"); }
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
          <button className="btn" onClick={handleRefreshProgress} disabled={refreshing}>
            {refreshing ? "刷新中…" : "↻ 刷新进度"}
          </button>
          <button className="btn primary" onClick={() => { setCreating(true); setTimeout(() => document.getElementById("new-goal")?.scrollIntoView({ behavior: "smooth" }), 0); }}>＋ 新建目标</button>
        </div>
      </div>

      <GoalHero
        goals={goals}
        summary={summary}
        personaKeywords={cfgView?.readonly.persona_keywords}
        forbiddenDirections={cfgView?.readonly.forbidden_directions}
        onCreateGoal={() => { setCreating(true); setTimeout(() => document.getElementById("new-goal")?.scrollIntoView({ behavior: "smooth" }), 0); }}
      />

      <div className="tabs page-tabs">
        {GOAL_TABS.map((t) => (
          <button key={t.k} type="button" className={`tab ${tab === t.k ? "active" : ""}`} onClick={() => setTab(t.k)}>{t.l}</button>
        ))}
      </div>

      {tab === "persona" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">◉</span>账号定位与画像</div>
              <div className="panel-sub">不同平台登录的可以是不同定位的账号；选择目标模式后，为每个平台单独设置画像与协同关系</div>
            </div>
          </div>
          <PersonaMatrix onSaved={load} />
        </div>
      )}

      {tab === "progress" && (
      <div className="grid two">
        {/* 平台阶段目标：每个平台独立，只统计该平台自己的目标 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">◎</span>平台阶段目标</div>
              <div className="panel-sub">每个平台的目标单独计算；全平台目标见上方关键结果</div>
            </div>
          </div>
          <div className="grid two">
            {ALL_PLATFORMS.map((p) => {
              const m = platformMeta(p);
              const pGoals = goals.filter((g) => isActiveGoal(g) && g.target_platform === p);
              return (
                <div key={p} className="platform-goal">
                  <div className="platform-goal-head">
                    <div className="platform-name">
                      <span className={`platform-logo ${PLATFORM_CLS[p]}`} aria-hidden="true">{m.label.slice(0, 1)}</span>
                      {m.label}
                    </div>
                    <span className="tag gray">
                      {summary?.by_platform?.[p]?.percent != null ? `${Math.round(summary.by_platform[p].percent as number)}% · ` : ""}{pGoals.length} 项
                    </span>
                  </div>
                  {mode !== "unified" && personas?.platforms?.[p] && (
                    <div className="tiny" style={{ marginTop: 4 }}>
                      {personas.platforms[p].effective.account_name ? `${personas.platforms[p].effective.account_name} · ` : ""}
                      {personas.platforms[p].effective.positioning || "沿用全局定位"}
                      {personas.platforms[p].effective.role ? ` · ${ROLE_LABELS[personas.platforms[p].effective.role]}` : ""}
                    </div>
                  )}
                  {pGoals.some((g) => g.target_metric === "followers_gained" || g.target_metric === "new_followers") && (
                    <button type="button" className="btn sm" style={{ marginTop: 6 }} onClick={() => recordFollowers(p)}>记录当前粉丝数</button>
                  )}
                  {pGoals.length === 0 ? (
                    <div className="tiny">未设置阶段目标</div>
                  ) : pGoals.map((g) => (
                    <div key={g.id} style={{ marginTop: 8 }}>
                      <div className="goal-main">
                        <b>{g.achievement_rate == null ? "—" : `${Math.round(g.achievement_rate)}%`}</b>
                        <span>{metricLabel(g.target_metric)} · 目标 {formatMetricValue(g.target_metric, g.target_value)} · 当前 {currentProgressText(g)}</span>
                      </div>
                      <div className="progress">
                        <span style={{ width: `${Math.min(100, Math.max(0, g.achievement_rate ?? 0))}%` }} />
                      </div>
                      {(g.contribution_note || (g.parent_goal_id && goalById[g.parent_goal_id])) && (
                        <div className="tiny" style={{ marginTop: 4 }}>
                          ↗ {g.contribution_note || `服务于「${goalById[g.parent_goal_id as string]?.goal_text}」`}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        </div>

        {/* 策略与约束（全部来自 /api/config） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon" aria-hidden="true">⚑</span>策略与约束</div>
              <div className="panel-sub">目标模式、定位、红线与平台协同；在「账号定位与画像」页签修改</div>
            </div>
            <button className="btn sm" onClick={() => setTab("persona")}>修改 →</button>
          </div>
          {!cfgView ? (
            <div className="note">加载配置中…</div>
          ) : (
            <div className="list">
              <div className="task-row">
                <i className="task-prio blue" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">目标模式 · {GOAL_MODE_META[mode as keyof typeof GOAL_MODE_META]?.title ?? mode}</div>
                  <div className="task-desc">
                    {GOAL_MODE_META[mode as keyof typeof GOAL_MODE_META]?.desc}
                    {mode === "hybrid" && (summary?.north_star ? ` · 总目标：${summary.north_star.goal_text}` : " · 还没有总目标，新建目标时选择「总目标」")}
                  </div>
                </div>
              </div>
              {mode !== "unified" && (personas?.links?.length ?? 0) > 0 && (
                <div className="task-row">
                  <i className="task-prio purple" aria-hidden="true" />
                  <div className="task-main">
                    <div className="task-title">平台协同</div>
                    <div className="task-desc">
                      {personas!.links.map((l) => `${platformMeta(l.source).label} ${RELATION_LABELS[l.relation]} ${platformMeta(l.target).label}`).join("；")}
                    </div>
                  </div>
                </div>
              )}
              <div className="task-row">
                <i className="task-prio green" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">{mode === "unified" ? "账号定位关键词" : "全局定位关键词"}</div>
                  <div className="flex wrap" style={{ gap: 6, marginTop: 4 }}>
                    {cfgView.readonly.persona_keywords.length === 0
                      ? <span className="tag gray">未配置</span>
                      : cfgView.readonly.persona_keywords.map((k) => <span key={k} className="tag blue">{k}</span>)}
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">禁止方向</div>
                  <div className="flex wrap" style={{ gap: 6, marginTop: 4 }}>
                    {cfgView.readonly.forbidden_directions.length === 0
                      ? <span className="tag gray">未配置</span>
                      : cfgView.readonly.forbidden_directions.map((k) => <span key={k} className="tag red">{k}</span>)}
                  </div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio red" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">红线敏感词库</div>
                  <div className="task-desc">已配置 {cfgView.readonly.redline_keywords_count} 条</div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio blue" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">评分模式</div>
                  <div className="task-desc">当前 {({ cold_start: "冷启动", sample_accumulating: "样本积累", stable: "稳定运行", stable_operation: "稳定运行" } as Record<string, string>)[status?.scoring_mode ?? ""] ?? "—"} · 由已发布样本量自动切换</div>
                </div>
              </div>
              <div className="task-row">
                <i className="task-prio" aria-hidden="true" />
                <div className="task-main">
                  <div className="task-title">LLM 模型</div>
                  <div className="task-desc">
                    {cfgView.readonly.llm_model || "—"} · API Key {cfgView.readonly.llm_has_api_key ? "已配置" : "未配置"}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      )}

      {creating && (
      <div className="panel" id="new-goal" style={{ borderColor: "var(--blue)" }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon" aria-hidden="true">＋</span>新建阶段目标</div>
            <div className="panel-sub">指标、目标值、口径、基线、统计周期和数据来源都要填写，进度才有计算依据</div>
          </div>
          <div className="head-actions"><button className="btn sm" onClick={() => setCreating(false)}>收起</button></div>
        </div>
        <div className="grid four" style={{ gap: 10 }}>
          <div>
            <label className="form-label">目标描述 *</label>
            <input className="field" placeholder="如：小红书涨粉 500"
              value={newGoal.goal_text}
              onChange={(e) => {
                const text = e.target.value;
                const detected = detectPlatform(text);
                setNewGoal((g) => ({
                  ...g,
                  goal_text: text,
                  // 用户没手动改过平台时，按描述里的平台名自动选中
                  target_platform: !platformTouched && detected ? detected : g.target_platform,
                }));
              }} />
          </div>
          <div>
            <label className="form-label">指标 *</label>
            <select className="field" value={newGoal.target_metric} onChange={(e) => setNewGoal({ ...newGoal, target_metric: e.target.value })}>
              {METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </div>
          <div>
            <label className="form-label">目标值 *{PERCENT_METRICS.has(newGoal.target_metric) ? "（单位 %，填 4 表示 4%）" : ""}</label>
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
            <label className="form-label">目标层级</label>
            <select className="field" value={newGoal.goal_level} onChange={(e) => setNewGoal({ ...newGoal, goal_level: e.target.value })}>
              <option value="platform">平台目标</option>
              <option value="north_star" disabled={mode === "per_platform"}>总目标（账号矩阵北极星）</option>
            </select>
          </div>
          {newGoal.goal_level === "platform" && (
            <div>
              <label className="form-label">目标平台 *（决定统计口径）</label>
              <select className="field" value={newGoal.target_platform}
                onChange={(e) => { setPlatformTouched(true); setNewGoal({ ...newGoal, target_platform: e.target.value }); }}>
                <option value="" disabled>请选择平台</option>
                {ALL_PLATFORMS.map((p) => (
                  <option key={p} value={p}>{platformMeta(p).label}</option>
                ))}
                <option value="all">全平台（跨平台汇总）</option>
              </select>
              <div className="tiny" style={{ marginTop: 4 }}>
                {newGoal.target_platform === "all"
                  ? "汇总各平台数据：基线要填各平台合计值，涨粉类目标按各平台粉丝数之和计算"
                  : newGoal.target_platform
                    ? `只统计「${platformMeta(newGoal.target_platform as Platform).label}」的数据，涨粉目标用该平台粉丝数快照`
                    : "未选平台时无法确定只统计哪个平台，进度会算错"}
              </div>
            </div>
          )}
          {newGoal.goal_level === "platform" && northStars.length > 0 && mode !== "per_platform" && (
            <>
              <div>
                <label className="form-label">服务于哪个总目标</label>
                <select className="field" value={newGoal.parent_goal_id} onChange={(e) => setNewGoal({ ...newGoal, parent_goal_id: e.target.value })}>
                  <option value="">不挂靠</option>
                  {northStars.map((g) => <option key={g.id} value={g.id}>{g.goal_text}</option>)}
                </select>
              </div>
              <div>
                <label className="form-label">汇总权重（空 = 1）</label>
                <input className="field" type="number" min={0.1} step={0.5} value={newGoal.weight}
                  onChange={(e) => setNewGoal({ ...newGoal, weight: e.target.value })} />
              </div>
            </>
          )}
          {newGoal.goal_level === "platform" && (
            <div style={{ gridColumn: "span 2" }}>
              <label className="form-label">如何促进总目标 / 其它平台（选填）</label>
              <input className="field" placeholder="如：小红书种草，为视频号直播导流" value={newGoal.contribution_note}
                onChange={(e) => setNewGoal({ ...newGoal, contribution_note: e.target.value })} />
            </div>
          )}
        </div>
        <button className="btn primary" style={{ marginTop: 12 }} onClick={handleCreate} disabled={submitting}>
          {submitting ? "提交中…" : "创建目标"}
        </button>
      </div>
      )}

      {editing && (
        <div className="panel" style={{ borderColor: "var(--blue)" }}>
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">✎</span>编辑目标 · {editing.goal_text}</div>
              <div className="panel-sub">可改口径 / 基线 / 周期 / 状态；当前值只由数据采集回写，不能手填</div>
            </div>
            <div className="head-actions">
              <button className="btn sm" onClick={() => setEditing(null)}>取消</button>
            </div>
          </div>
          <div className="grid four" style={{ gap: 10 }}>
            <div>
              <label className="form-label">目标描述 *</label>
              <input className="field" value={editing.goal_text}
                onChange={(e) => setEditing({ ...editing, goal_text: e.target.value })} />
            </div>
            <div>
              <label className="form-label">指标 *</label>
              <select className="field" value={editing.target_metric}
                onChange={(e) => setEditing({ ...editing, target_metric: e.target.value })}>
                {METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div>
              <label className="form-label">目标值 *</label>
              <input className="field" type="number" min={1} value={editing.target_value}
                onChange={(e) => setEditing({ ...editing, target_value: Number(e.target.value) })} />
            </div>
            <div>
              <label className="form-label">口径 *</label>
              <select className="field" value={editing.metric_scope}
                onChange={(e) => setEditing({ ...editing, metric_scope: e.target.value })}>
                <option value="delta">增量（本阶段新增）</option>
                <option value="total">总量（累计达到）</option>
              </select>
            </div>
            <div>
              <label className="form-label">基线值</label>
              <input className="field" type="number" value={editing.baseline_value ?? ""}
                onChange={(e) => setEditing({ ...editing, baseline_value: e.target.value === "" ? null : Number(e.target.value) })} />
            </div>
            <div>
              <label className="form-label">基线时间 *</label>
              <input className="field" type="date" value={editing.baseline_at ?? ""}
                onChange={(e) => setEditing({ ...editing, baseline_at: e.target.value })} />
            </div>
            <div>
              <label className="form-label">周期开始 *</label>
              <input className="field" type="date" value={editing.metric_period_start ?? ""}
                onChange={(e) => setEditing({ ...editing, metric_period_start: e.target.value })} />
            </div>
            <div>
              <label className="form-label">周期结束 *</label>
              <input className="field" type="date" value={editing.metric_period_end ?? ""}
                onChange={(e) => setEditing({ ...editing, metric_period_end: e.target.value })} />
            </div>
            <div>
              <label className="form-label">数据来源 *</label>
              <select className="field" value={editing.data_source ?? ""}
                onChange={(e) => setEditing({ ...editing, data_source: e.target.value })}>
                {DATA_SOURCES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </select>
            </div>
            <div>
              <label className="form-label">目标平台 *（决定统计口径）</label>
              <select className="field" value={editing.target_platform ?? ""}
                onChange={(e) => setEditing({ ...editing, target_platform: (e.target.value || null) as any })}>
                <option value="">全平台（跨平台汇总）</option>
                <option value="xhs">小红书</option>
                <option value="douyin">抖音</option>
                <option value="kuaishou">快手</option>
                <option value="wechat_channels">视频号</option>
              </select>
            </div>
            <div>
              <label className="form-label">状态</label>
              <select className="field" value={editing.status}
                onChange={(e) => setEditing({ ...editing, status: e.target.value })}>
                {Object.entries(GOAL_STATUS_LABEL).filter(([k]) => k !== "expired").map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
          </div>
          <button className="btn primary" style={{ marginTop: 12 }} onClick={handleUpdate} disabled={savingEdit}>
            {savingEdit ? "保存中…" : "保存修改"}
          </button>
        </div>
      )}

      {tab === "list" && (
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◎</span>所有目标</div>
            <div className="panel-sub">达成率：增量 =（当前值 − 基线）/ 目标值，总量 = 当前值 / 目标值；任一分量缺失即显示「数据待采集」（不显示 0%）</div>
          </div>
        </div>
        {error && (
          <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
            <span>! {error}</span><button className="btn sm" onClick={load}>重试</button>
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>目标</th><th>平台</th><th>指标</th><th>目标值</th>
                <th>当前值</th><th>口径</th><th>周期</th><th>达成率</th><th>状态</th><th style={{ width: 140 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {goals.length === 0 && (
                <tr><td colSpan={10}>
                  <EmptyHint className="" next="用上方「新建阶段目标」表单创建第一个阶段目标">暂无目标</EmptyHint>
                </td></tr>
              )}
              {goals.map((g) => {
                const rate = g.achievement_rate;
                const progress = rate == null ? null : Math.max(0, Math.min(100, rate));
                return (
                  <tr key={g.id}>
                    <td style={{ fontWeight: 600 }}>{g.goal_text}</td>
                    <td>{g.goal_level === "north_star" ? <span className="tag purple">总目标</span> : g.target_platform ? platformMeta(g.target_platform).label : "全平台"}</td>
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
                    <td><span className={`tag ${g.status === "active" ? "green" : g.status === "completed" ? "blue" : "gray"}`}>{GOAL_STATUS_LABEL[g.status] ?? g.status}</span></td>
                    <td>
                      <div className="flex" style={{ gap: 4 }}>
                        <button className="btn sm" onClick={() => setEditing(g)}>编辑</button>
                        {g.data_source === "manual" && <button className="btn sm" onClick={() => fillManualValue(g)}>填当前值</button>}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      )}
    </>
  );
}
