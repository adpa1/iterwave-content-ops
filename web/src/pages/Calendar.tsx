import { useEffect, useMemo, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import { api, ScheduleEvent, UnscheduledDraft } from "../api";
import { platformMeta } from "../platforms";

export default function Calendar() {
  const { showToast, withPending, isPending } = useApp();
  const today = new Date();
  const year = today.getFullYear();
  const month = today.getMonth();

  const [events, setEvents] = useState<ScheduleEvent[]>([]);
  const [unscheduled, setUnscheduled] = useState<UnscheduledDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 行内排期编辑：selected 为草稿 id，draftTime 为 datetime-local 值
  const [selected, setSelected] = useState<{ id: string; title: string } | null>(null);
  const [draftTime, setDraftTime] = useState("");
  const [pendingDraft, setPendingDraft] = useState<Record<string, string>>({});

  // 本周 7 天（以周日为周首）
  const startOfWeek = useMemo(() => {
    const d = new Date(today);
    d.setDate(today.getDate() - today.getDay());
    return d;
  }, [today.toDateString()]);
  const endOfWeek = useMemo(() => {
    const d = new Date(startOfWeek);
    d.setDate(startOfWeek.getDate() + 6);
    return d;
  }, [startOfWeek.toDateString()]);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.schedule(
        _fmt(startOfWeek),
        _fmt(new Date(endOfWeek.getTime() + 86_400_000)), // 包含周日
      );
      setEvents(r.items);
      setUnscheduled(r.unscheduled ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const platformColors: Record<string, string> = {
    xhs: "#fe2c55", douyin: "#000", wechat_channels: "#07c160", kuaishou: "#ff6a00",
  };
  const statusLabel: Record<string, string> = {
    draft_pending: "待存草稿",
    draft_saved: "草稿已存",
    user_published: "用户确认",
    published: "已发布",
  };

  const days: { date: Date; key: string; events: ScheduleEvent[] }[] = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(startOfWeek);
    d.setDate(startOfWeek.getDate() + i);
    const key = _fmt(d);
    days.push({
      date: d,
      key,
      events: events.filter((e) => _eventDate(e) === key),
    });
  }

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
          <div className="eyebrow">PUBLISH CALENDAR</div>
          <h1 className="page-title">内容日历与排期</h1>
          <div className="page-desc">
            以「内容 × 平台 × 发布时间」为最小排期单元。系统永不自动触发发布动作。
            {events.length > 0 && loading === false && (
              <span className="tag teal" style={{ marginLeft: 8 }}>
                本周 {events.length} 条
              </span>
            )}
          </div>
        </div>
        <div className="head-actions">
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
              <span className="icon">◷</span>{year} 年 {month + 1} 月 · 本周排期
            </div>
            <div className="panel-sub">
              按平台色标显示每篇草稿的计划发布时间；点击可修改排期。
            </div>
          </div>
        </div>
        <div className="grid" style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))", gap: 10 }}>
          {["日", "一", "二", "三", "四", "五", "六"].map((d, i) => (
            <div key={i} style={{ textAlign: "center", fontSize: 12, fontWeight: 600, color: "var(--muted)" }}>{d}</div>
          ))}
          {days.map((d, i) => {
            const isToday = d.date.toDateString() === today.toDateString();
            return (
              <div key={i} style={{
                border: "1px solid var(--line)", borderRadius: 10, padding: 10, minHeight: 120,
                background: isToday ? "var(--blue-weak)" : "#fff",
              }}>
                <div style={{
                  fontSize: 16, fontWeight: 700,
                  color: isToday ? "var(--blue)" : "var(--text)",
                }}>
                  {d.date.getDate()}
                </div>
                {loading && (
                  <div className="loading" style={{ marginTop: 6, fontSize: 11, color: "var(--muted)" }}>
                    <div className="spinner" />加载中...
                  </div>
                )}
                {!loading && d.events.length === 0 && (
                  <div style={{ marginTop: 6, fontSize: 11, color: "var(--subtle)" }}>空</div>
                )}
                {d.events.map((e, j) => {
                  // 只有 draft_pending / draft_saved 可改排期；其余状态点了会被后端 409
                  const editable = e.status === "draft_pending" || e.status === "draft_saved";
                  return (
                  <div
                    key={j}
                    title={editable
                      ? `${e.title} · 点击改排期`
                      : `${e.title} · ${statusLabel[e.status] ?? e.status}（不可改排期）`}
                    onClick={editable ? () => {
                      setSelected({ id: e.id, title: e.title });
                      setDraftTime(_toLocalInput(e.scheduled || e.saved_at));
                    } : undefined}
                    style={{
                      marginTop: 6, padding: "4px 8px", borderRadius: 6, fontSize: 11, color: "var(--text)",
                      background: "var(--surface-2)",
                      borderLeft: `3px solid ${platformColors[e.platform] || "#666"}`,
                      cursor: editable ? "pointer" : "not-allowed",
                      opacity: editable ? 1 : 0.62,
                      outline: selected?.id === e.id ? "2px solid var(--blue)" : "none",
                    }}
                  >
                    <div style={{ fontWeight: 600 }}>
                      {e.title.length > 16 ? e.title.slice(0, 15) + "…" : e.title}
                    </div>
                    <div style={{ color: "var(--muted)", marginTop: 2, fontSize: 10 }}>
                      {e.scheduled ? e.scheduled.slice(11, 16) : "未排期"}
                      {statusLabel[e.status] ? ` · ${statusLabel[e.status]}` : ""}
                    </div>
                  </div>
                  );
                })}
              </div>
            );
          })}
        </div>

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

function _fmt(d: Date): string {
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
