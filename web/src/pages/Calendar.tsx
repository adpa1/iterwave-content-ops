import { useEffect, useMemo, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import { api, ContentPlan, Platform, ScheduleEvent, UnscheduledDraft } from "../api";
import { platformMeta } from "../platforms";

export default function Calendar() {
  const { showToast, withPending, isPending } = useApp();
  const today = new Date();
  const [cursorDate, setCursorDate] = useState(() => new Date(today));
  const [viewMode, setViewMode] = useState<"month" | "week" | "day">("month");
  const year = cursorDate.getFullYear();
  const month = cursorDate.getMonth();

  const [events, setEvents] = useState<ScheduleEvent[]>([]);\n  const [plans, setPlans] = useState<ContentPlan[]>([]);\n  const [planTitle, setPlanTitle] = useState("");\n  const [planTopic, setPlanTopic] = useState("");\n  const [planDate, setPlanDate] = useState(_fmt(today));\n  const [planCount, setPlanCount] = useState("1");\n  const [planPlatforms, setPlanPlatforms] = useState<Platform[]>([]);\n  const [planNotes, setPlanNotes] = useState("");
  const [unscheduled, setUnscheduled] = useState<UnscheduledDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 行内排期编辑：selected 为草稿 id，draftTime 为 datetime-local 值
  const [selected, setSelected] = useState<{ id: string; title: string } | null>(null);
  const [draftTime, setDraftTime] = useState("");
  const [pendingDraft, setPendingDraft] = useState<Record<string, string>>({});

  // API 查询范围与当前视图一致：月视图覆盖完整日历网格，周/日视图只请求可见日期。
  const visibleStart = useMemo(() => {
    if (viewMode === "day") return new Date(year, month, cursorDate.getDate());
    if (viewMode === "week") {
      const d = new Date(cursorDate);
      d.setDate(d.getDate() - d.getDay());
      d.setHours(0, 0, 0, 0);
      return d;
    }
    const d = new Date(year, month, 1);
    d.setDate(1 - d.getDay());
    return d;
  }, [viewMode, year, month, cursorDate]);
  const visibleEndExclusive = useMemo(() => {
    if (viewMode === "day") {
      const d = new Date(visibleStart);
      d.setDate(d.getDate() + 1);
      return d;
    }
    if (viewMode === "week") {
      const d = new Date(visibleStart);
      d.setDate(d.getDate() + 7);
      return d;
    }
    const d = new Date(year, month + 1, 1);
    d.setDate(d.getDate() + (6 - d.getDay()) + 1);
    return d;
  }, [viewMode, visibleStart, year, month]);
  const days = useMemo(() => {
    const result: { date: Date; key: string; events: ScheduleEvent[]; plans: ContentPlan[]; inMonth: boolean }[] = [];
    for (let d = new Date(visibleStart); d < visibleEndExclusive; d.setDate(d.getDate() + 1)) {
      const date = new Date(d);
      const key = _fmt(date);
      result.push({ date, key, events: events.filter((event) => _eventDate(event) === key), plans: plans.filter((plan) => plan.scheduled_date === key && plan.status !== "cancelled" && plan.status !== "archived"), inMonth: date.getMonth() === month });
    }
    return result;
  }, [visibleStart, visibleEndExclusive, events, plans, month]);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [r, planResult] = await Promise.all([
        api.schedule(_fmt(visibleStart), _fmt(visibleEndExclusive)),
        api.contentPlans(_fmt(visibleStart), _fmt(new Date(visibleEndExclusive.getTime() - 86400000))),
      ]);
      setEvents(r.items);
      setPlans(planResult.items);
      setUnscheduled(r.unscheduled ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [viewMode, year, month, cursorDate]);

  const platformColors: Record<string, string> = {
    xhs: "#fe2c55", douyin: "#000", wechat_channels: "#07c160", kuaishou: "#ff6a00",
  };
  const statusLabel: Record<string, string> = {
    draft_pending: "待存草稿",
    draft_saved: "草稿已存",
    user_published: "用户确认",
    published: "已发布",
  };

  /** 保存排期：datetime-local 的值是 "YYYY-MM-DDTHH:MM"，后端按本地时间解释（R5） */
  const saveSchedule = async (draftId: string, value: string) => {
    if (!value) { showToast("请先选择排期时间", "error"); return; }
    try {
      await withPending(`schedule:${draftId}`, () => api.scheduleDraft(draftId, value));
      showToast("✓ 排期已保存（仅排期，不会触发发布）", "success");
      setSelected(null);
      setDraftTime("");
      setPendingDraft((prev) => { const n = { ...prev }; delete n[draftId]; return n; });
      await load();
    } catch (e: any) {
      showToast(`排期保存失败: ${e?.message ?? e}`, "error");
    }
  };

  const clearSchedule = async (draftId: string) => {
    try {
      await withPending(`schedule-clear:${draftId}`, () => api.updateSchedule(draftId, null));
      showToast("已取消排期", "success");
      setSelected(null);
      await load();
    } catch (e: any) {
      showToast(`取消失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">发布日历</div>
          <h1 className="page-title">内容日历与排期</h1>
          <div className="page-desc">
            以「内容 × 平台 × 发布时间」为最小排期单元。系统永不自动触发发布动作。
            {!loading && <span className="tag teal" style={{ marginLeft: 8 }}>
              当前{viewMode === "month" ? "月" : viewMode === "week" ? "周" : "日"} {events.length} 条
            </span>}
          </div>
        </div>
        <div className="head-actions calendar-toolbar">
          <div className="segmented-control" role="group" aria-label="日历视图">
            {(["month", "week", "day"] as const).map((mode) => (
              <button key={mode} className={`btn sm ${viewMode === mode ? "primary" : ""}`} aria-pressed={viewMode === mode} onClick={() => setViewMode(mode)}>
                {mode === "month" ? "月" : mode === "week" ? "周" : "日"}
              </button>
            ))}
          </div>
          <button className="btn sm" onClick={() => setCursorDate((d) => shiftCalendarDate(d, viewMode, -1))}>上一{viewMode === "month" ? "月" : viewMode === "week" ? "周" : "日"}</button>
          <button className="btn sm" onClick={() => setCursorDate(new Date(today))}>今天</button>
          <button className="btn sm" onClick={() => setCursorDate((d) => shiftCalendarDate(d, viewMode, 1))}>下一{viewMode === "month" ? "月" : viewMode === "week" ? "周" : "日"}</button>
          <input aria-label="选择日历日期" className="field" type="date" value={_fmt(cursorDate)} onChange={(e) => { if (e.target.value) { const [y, m, d] = e.target.value.split("-").map(Number); setCursorDate(new Date(y, m - 1, d)); } }} />
          <button className="btn sm" onClick={load} disabled={loading}>↻ 刷新</button>
        </div>
      </div>

      {error && <div className="panel" style={{ borderColor: "var(--red)" }}>
        <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
          <span>排期接口异常：{error}</span>
          <button className="btn sm" onClick={load}>↻ 重试</button>
        </div>
      </div>}

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">
              <span className="icon">◷</span>{viewMode === "month" ? `${year} 年 ${month + 1} 月` : viewMode === "week" ? `${_fmt(visibleStart)} — ${_fmt(new Date(visibleEndExclusive.getTime() - 86400000))}` : _fmt(cursorDate)} · {viewMode === "month" ? "月视图" : viewMode === "week" ? "周视图" : "日视图"}
            </div>
            <div className="panel-sub">
              月视图查看整月分布，周／日视图聚焦具体排期；点击可编辑草稿时间。排期不会触发自动发布。
            </div>
          </div>
        </div>
        {viewMode === "month" ? (
          <div className="calendar-month-scroll">
            <div className="calendar-month-grid" style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0, 1fr))", gap: 8 }}>
              {["日", "一", "二", "三", "四", "五", "六"].map((d, i) => (
                <div key={i} style={{ textAlign: "center", fontSize: 12, fontWeight: 600, color: "var(--muted)" }}>{d}</div>
              ))}
              {days.map((d) => {
                const isToday = d.date.toDateString() === today.toDateString();
                return (
                  <div key={d.key} style={{
                    border: "1px solid var(--line)", borderRadius: 10, padding: 10, minHeight: 120,
                    background: isToday ? "var(--blue-weak)" : "var(--surface)",
                    opacity: d.inMonth ? 1 : 0.48,
                  }}>
                    <button className="calendar-day-number" onClick={() => { setCursorDate(d.date); setViewMode("day"); }} aria-label={`查看 ${d.key}`}>
                      {d.date.getDate()}
                    </button>
                    {loading && <div className="loading" style={{ marginTop: 6, fontSize: 11, color: "var(--muted)" }}><div className="spinner" />加载中...</div>}
                    {!loading && d.events.length === 0 && <div style={{ marginTop: 6, fontSize: 11, color: "var(--subtle)" }}>空</div>}
                    {d.plans.map((plan) => (
                      <div key={plan.id} className="calendar-plan-chip" title={`${plan.title} · ${plan.planned_count} 篇计划`}>
                        <strong>计划</strong><span>{plan.title}</span>
                      </div>
                    ))}
                    {d.events.map((e) => {
                      const editable = e.status === "draft_pending" || e.status === "draft_saved";
                      return (
                        <button type="button" key={e.id} className="calendar-event"
                          title={editable ? `${e.title} · 点击改排期` : `${e.title} · ${statusLabel[e.status] ?? e.status}（不可改排期）`}
                          disabled={!editable}
                          onClick={() => { setSelected({ id: e.id, title: e.title }); setDraftTime(_toLocalInput(e.scheduled || e.saved_at)); }}
                          style={{ borderLeft: `3px solid ${platformColors[e.platform] || "#666"}`, outline: selected?.id === e.id ? "2px solid var(--blue)" : "none" }}>
                          <span className="calendar-event-title">{e.title}</span>
                          <span className="calendar-event-meta">{e.scheduled ? e.scheduled.slice(11, 16) : "未排期"}{statusLabel[e.status] ? ` · ${statusLabel[e.status]}` : ""}</span>
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="calendar-agenda">
            {days.map((d) => (
              <section className="calendar-agenda-day" key={d.key}>
                <button className="calendar-agenda-date" onClick={() => { setCursorDate(d.date); setViewMode("day"); }}>
                  <strong>{d.date.toLocaleDateString("zh-CN", { weekday: "long" })}</strong>
                  <span>{d.key}{d.date.toDateString() === today.toDateString() ? " · 今天" : ""}</span>
                </button>
                {d.plans.map((plan) => (
                  <div className="calendar-agenda-plan" key={plan.id}>
                    <span className="calendar-plan-label">计划</span>
                    <span className="calendar-agenda-copy"><strong>{plan.title}</strong><small>{plan.planned_count} 篇 · {plan.platforms.length ? plan.platforms.map((p) => platformMeta(p).label).join("、") : "未指定平台"} · {plan.status}</small></span>
                    <button className="btn sm" onClick={async () => { try { await api.cancelContentPlan(plan.id, plan.version); showToast("计划已取消", "success"); await load(); } catch (e: any) { showToast(`取消失败：${e?.message ?? e}`, "error"); } }}>取消</button>
                  </div>
                ))}
                {loading ? <div className="loading"><div className="spinner" />加载中...</div> : d.events.length === 0 && d.plans.length === 0 ? (
                  <div className="calendar-agenda-empty">当天没有排期</div>
                ) : d.events.map((e) => {
                  const editable = e.status === "draft_pending" || e.status === "draft_saved";
                  return (
                    <button type="button" key={e.id} className="calendar-agenda-event" disabled={!editable}
                      onClick={() => { setSelected({ id: e.id, title: e.title }); setDraftTime(_toLocalInput(e.scheduled || e.saved_at)); }}>
                      <span className="calendar-agenda-time">{e.scheduled ? e.scheduled.slice(11, 16) : "—"}</span>
                      <span className="calendar-agenda-color" style={{ background: platformColors[e.platform] || "#666" }} />
                      <span className="calendar-agenda-copy"><strong>{e.title}</strong><small>{platformMeta(e.platform).label} · {statusLabel[e.status] ?? e.status}</small></span>
                      <span className="calendar-agenda-action">{editable ? "编辑排期" : "不可编辑"}</span>
                    </button>
                  );
                })}
              </section>
            ))}
          </div>
        )}

        {selected && (
          <div className="panel" style={{ marginTop: 14, borderColor: "var(--blue)" }}>
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">✎</span>排期编辑 · {selected.title}</div>
                <div className="panel-sub">仅保存计划发布时间，不会自动触发平台发布</div>
              </div>
              <div className="head-actions" style={{ display: "flex", gap: 8 }}>
                <input
                  className="field"
                  type="datetime-local"
                  value={draftTime}
                  onChange={(e) => {
                    setDraftTime(e.target.value);
                    setPendingDraft((p) => ({ ...p, [selected.id]: e.target.value }));
                  }}
                  style={{ width: 220 }}
                />
                <button
                  className="btn primary sm"
                  disabled={isPending(`schedule:${selected.id}`)}
                  onClick={() => saveSchedule(selected.id, draftTime)}
                >
                  {isPending(`schedule:${selected.id}`) ? "保存中…" : "保存排期"}
                </button>
                <button
                  className="btn sm"
                  disabled={isPending(`schedule-clear:${selected.id}`)}
                  onClick={() => clearSchedule(selected.id)}
                >
                  取消排期
                </button>
                <button className="btn sm" onClick={() => { setSelected(null); setDraftTime(""); }}>
                  关闭
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <section className="panel calendar-plan-create">
        <div className="panel-head">
          <div><div className="panel-title">内容计划</div><div className="panel-sub">计划独立于草稿保存，可先安排主题和产出数量；创建计划不会生成内容或发布。</div></div>
        </div>
        <div className="calendar-plan-form">
          <label>计划名称<input className="field" value={planTitle} maxLength={160} onChange={(e) => setPlanTitle(e.target.value)} placeholder="例如：本周产品功能系列" /></label>
          <label>选题／主题<input className="field" value={planTopic} maxLength={500} onChange={(e) => setPlanTopic(e.target.value)} placeholder="填写计划主题或核心选题" /></label>
          <label>计划日期<input className="field" type="date" value={planDate} onChange={(e) => setPlanDate(e.target.value)} /></label>
          <label>计划篇数<input className="field" type="number" min="1" max="100" value={planCount} onChange={(e) => setPlanCount(e.target.value)} /></label>
          <label className="calendar-plan-platforms">目标平台
            <span>{(["xhs", "douyin", "wechat_channels", "kuaishou"] as Platform[]).map((platform) => (
              <label key={platform} className="calendar-plan-check"><input type="checkbox" checked={planPlatforms.includes(platform)} onChange={(e) => setPlanPlatforms((old) => e.target.checked ? [...old, platform] : old.filter((p) => p !== platform))} />{platformMeta(platform).label}</label>
            ))}</span>
          </label>
          <label className="calendar-plan-notes">备注<textarea className="field" rows={2} value={planNotes} maxLength={4000} onChange={(e) => setPlanNotes(e.target.value)} placeholder="可选" /></label>
          <button className="btn primary" disabled={!planTitle.trim() || !planDate || Number(planCount) < 1 || Number(planCount) > 100 || isPending("content-plan:create")} onClick={async () => {
            try {
              await withPending("content-plan:create", () => api.createContentPlan({ title: planTitle.trim(), topic: planTopic.trim(), platforms: planPlatforms, planned_count: Number(planCount), scheduled_date: planDate, notes: planNotes.trim() }));
              showToast("内容计划已创建", "success");
              setPlanTitle(""); setPlanTopic(""); setPlanCount("1"); setPlanPlatforms([]); setPlanNotes("");
              setCursorDate(new Date(`${planDate}T12:00:00`));
              await load();
            } catch (e: any) { showToast(`创建计划失败：${e?.message ?? e}`, "error"); }
          }}>{isPending("content-plan:create") ? "创建中…" : "创建计划"}</button>
        </div>
      </section>

      {/* 未排期草稿：直接挑选时间写入排期 */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◷</span>未排期草稿 · {unscheduled.length} 篇</div>
            <div className="panel-sub">还没有发布时间的草稿</div>
          </div>
        </div>
        {unscheduled.length === 0 ? (
          <EmptyHint next="到「内容生产 → 生产线」运行一轮并存草稿，草稿会出现在这里">当前没有待排期草稿</EmptyHint>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>类型</th><th>标题</th><th>平台</th><th>状态</th><th>排期时间</th><th>操作</th></tr>
              </thead>
              <tbody>
                {unscheduled.map((d) => (
                  <tr key={d.id}>
                    <td style={{ fontSize: 11, color: "var(--subtle)" }}>草稿</td>
                    <td>{d.title ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                    <td>
                      <span className="tag" style={{ background: platformColors[d.platform], color: "#fff" }}>
                        {platformMeta(d.platform).label}
                      </span>
                    </td>
                    <td>{statusLabel[d.status] ?? d.status}</td>
                    <td>
                      <input
                        className="field"
                        type="datetime-local"
                        value={pendingDraft[d.id] ?? ""}
                        onChange={(e) => setPendingDraft((p) => ({ ...p, [d.id]: e.target.value }))}
                        style={{ width: 200 }}
                      />
                    </td>
                    <td>
                      <button
                        className="btn primary sm"
                        disabled={isPending(`schedule:${d.id}`)}
                        onClick={() => saveSchedule(d.id, pendingDraft[d.id] ?? "")}
                      >
                        {isPending(`schedule:${d.id}`) ? "保存中…" : "排期"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function shiftCalendarDate(date: Date, mode: "month" | "week" | "day", direction: number): Date {\n  const next = new Date(date);\n  if (mode === "month") next.setMonth(next.getMonth() + direction);\n  else next.setDate(next.getDate() + (mode === "week" ? 7 : 1) * direction);\n  return next;\n}\n\nfunction _fmt(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function _eventDate(e: ScheduleEvent): string {
  const raw = e.scheduled || e.saved_at || "";
  // ISO 含时间部分 —— 只取日期
  return raw.slice(0, 10);
}

/** ISO / 日期串 → datetime-local 需要的 "YYYY-MM-DDTHH:MM"；无时间部分补 09:00 */
function _toLocalInput(raw: string | null): string {
  if (!raw) return "";
  const text = raw.replace(" ", "T");
  if (text.length >= 16) return text.slice(0, 16);
  return `${text.slice(0, 10)}T09:00`;
}
