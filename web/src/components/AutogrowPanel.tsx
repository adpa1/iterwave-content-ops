/**
 * 自动闭环（自生长）面板：状态 / 为什么现在不开轮 / 下一轮会吃进的复盘结论 / 参数在线修改 / 最近记录。
 */
import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { navigate } from "../nav";
import { api, AutogrowStatus } from "../api";

const ACTION_LABEL: Record<string, { text: string; cls: string }> = {
  start: { text: "可以开轮", cls: "green" },
  started: { text: "已开轮", cls: "green" },
  skipped: { text: "暂不开轮", cls: "gray" },
  stopped: { text: "已停止", cls: "orange" },
  failed: { text: "失败", cls: "red" },
};

export default function AutogrowPanel() {
  const { showToast, withPending, isPending } = useApp();
  const [st, setSt] = useState<AutogrowStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState<Record<string, string | boolean>>({});

  const load = async () => {
    setErr(null);
    try {
      const s = await api.autogrow();
      setSt(s);
      setForm({
        enabled: s.config.enabled, apply_feedback: s.config.apply_feedback,
        stop_when_goals_achieved: s.config.stop_when_goals_achieved,
        cooldown_hours: String(s.config.cooldown_hours), max_rounds_per_day: String(s.config.max_rounds_per_day),
        max_consecutive_failures: String(s.config.max_consecutive_failures), topic_count: String(s.config.topic_count),
      });
    } catch (e: any) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  const save = () => withPending("autogrow:save", async () => {
    if (!st) return;
    const updates: Record<string, unknown> = {};
    for (const k of ["enabled", "apply_feedback", "stop_when_goals_achieved"] as const) {
      if (form[k] !== st.config[k]) updates[`autogrow.${k}`] = form[k];
    }
    for (const k of ["cooldown_hours", "max_rounds_per_day", "max_consecutive_failures", "topic_count"] as const) {
      const n = Number(form[k]);
      if (!Number.isInteger(n)) { showToast(`${k} 必须是整数`, "error"); return; }
      if (n !== st.config[k]) updates[`autogrow.${k}`] = n;
    }
    if (!Object.keys(updates).length) { showToast("没有改动"); return; }
    try { await api.patchConfig(updates); showToast("✅ 自动闭环设置已保存", "success"); await load(); }
    catch (e: any) { showToast(`保存失败：${e.message}`, "error"); }
  });

  const runNow = () => withPending("autogrow:run", async () => {
    try {
      const r = await api.autogrowRun();
      showToast(r.reason, r.action === "started" ? "success" : "info");
      await load();
    } catch (e: any) { showToast(`执行失败：${e.message}`, "error"); }
  });

  if (err) return <div className="panel"><div className="note" style={{ color: "var(--red)" }}>自动闭环状态加载失败：{err}</div></div>;
  if (!st) return <div className="panel"><div className="note">加载自动闭环状态…</div></div>;

  const v = ACTION_LABEL[st.verdict.action] ?? { text: st.verdict.action, cls: "gray" };
  const bias = Object.entries(st.feedback.dimension_bias);
  const hints = Object.values(st.feedback.copy_hints).reduce((a, b) => a + b.length, 0);

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">🌱</span>自动闭环（自生长）</div>
          <div className="panel-sub">复盘结论 → 下一轮选题与文案；条件满足时自动开下一轮并生成草稿，发布仍由你确认</div>
        </div>
        <div className="flex" style={{ gap: 8 }}>
          <button className="btn sm" onClick={() => navigate("production", "plan")}>母计划节奏</button>
          <button className="btn sm primary" onClick={runNow} disabled={isPending("autogrow:run")}
            title="跳过冷却期与每日上限；活跃轮次、目标达成、连续失败、全平台熔断仍会拦住">
            {isPending("autogrow:run") ? "检查中…" : "▶ 立即检查并开轮"}
          </button>
        </div>
      </div>

      <div className="grid three" style={{ marginBottom: 12 }}>
        <div className="metric">
          <div className="metric-label">当前判定</div>
          <div className="metric-val" style={{ fontSize: 16 }}><span className={`tag ${v.cls}`}>{v.text}</span></div>
          <div className="metric-foot">{st.verdict.reason}{st.verdict.next_eligible_at ? ` · 最早 ${st.verdict.next_eligible_at.replace("T", " ")}` : ""}</div>
        </div>
        <div className="metric">
          <div className="metric-label">下一轮吃进的复盘结论</div>
          <div className="metric-val" style={{ fontSize: 16 }}>{bias.length} 个维度 · {hints} 条要点</div>
          <div className="metric-foot">
            {st.feedback.source_round_ids.length ? `来自第 ${st.feedback.source_round_ids.join("、")} 号轮` : "还没有已收口的复盘"}
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">下次自动检查</div>
          <div className="metric-val" style={{ fontSize: 16 }}>
            {st.job_enabled && st.next_check_at ? new Date(st.next_check_at).toLocaleString("zh-CN") : "定时任务已关闭"}
          </div>
          <div className="metric-foot">频率在下方「自动闭环」定时任务里改</div>
        </div>
      </div>

      {bias.length > 0 && (
        <div className="flex wrap" style={{ gap: 6, marginBottom: 12 }}>
          {bias.map(([d, b]) => (
            <span key={d} className={`tag ${b > 0 ? "green" : "red"}`} title={(st.feedback.bias_reasons[d] ?? []).join("；")}>
              {d} {b > 0 ? "+" : ""}{b}
            </span>
          ))}
        </div>
      )}

      <form className="toolbar" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          <input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />自动开轮
        </label>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          <input type="checkbox" checked={!!form.apply_feedback} onChange={(e) => setForm({ ...form, apply_feedback: e.target.checked })} />复盘结论进评分/文案
        </label>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          <input type="checkbox" checked={!!form.stop_when_goals_achieved} onChange={(e) => setForm({ ...form, stop_when_goals_achieved: e.target.checked })} />目标达成即停
        </label>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          冷却 <input className="field" type="number" min={0} max={720} style={{ width: 64 }} value={String(form.cooldown_hours ?? "")}
            onChange={(e) => setForm({ ...form, cooldown_hours: e.target.value })} /> 小时
        </label>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          每天最多 <input className="field" type="number" min={0} max={24} style={{ width: 56 }} value={String(form.max_rounds_per_day ?? "")}
            onChange={(e) => setForm({ ...form, max_rounds_per_day: e.target.value })} /> 轮
        </label>
        <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
          连续失败 <input className="field" type="number" min={1} max={20} style={{ width: 56 }} value={String(form.max_consecutive_failures ?? "")}
            onChange={(e) => setForm({ ...form, max_consecutive_failures: e.target.value })} /> 轮暂停
        </label>
        <button type="submit" className="btn sm primary" disabled={isPending("autogrow:save")}>保存</button>
      </form>
      <div className="note" style={{ marginTop: 6 }}>母计划设置了「每 N 天一轮」时以母计划为准，冷却时间不生效。</div>

      {st.recent_runs.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--muted)" }}>最近 {st.recent_runs.length} 次检查记录</summary>
          <div className="table-wrap" style={{ marginTop: 6 }}>
            <table>
              <thead><tr><th style={{ width: 150 }}>时间</th><th style={{ width: 80 }}>结果</th><th>原因</th></tr></thead>
              <tbody>
                {st.recent_runs.map((r) => (
                  <tr key={r.id}>
                    <td style={{ fontSize: 11, color: "var(--muted)" }}>{r.ran_at}</td>
                    <td><span className={`tag ${(ACTION_LABEL[r.action] ?? { cls: "gray" }).cls}`}>{(ACTION_LABEL[r.action] ?? { text: r.action }).text}</span></td>
                    <td style={{ fontSize: 12 }}>{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
