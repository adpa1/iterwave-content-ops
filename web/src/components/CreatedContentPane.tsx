/**
 * 已生产内容：按状态 / 平台筛选，单条或批量删除（已发布有数据的不能删）。
 */
import { useEffect, useMemo, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "./EmptyHint";
import { navigate } from "../nav";
import { api, Draft, platformMeta } from "../api";

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  draft_pending: { text: "待存草稿", cls: "gray" },
  draft_saved: { text: "已存草稿", cls: "blue" },
  user_published: { text: "已发布", cls: "green" },
  data_fetching: { text: "采集中", cls: "green" },
  published: { text: "已发布·有数据", cls: "green" },
  failed: { text: "失败", cls: "red" },
  cancelled: { text: "已取消", cls: "gray" },
  expired: { text: "已过期", cls: "orange" },
};
const LOCKED = new Set(["user_published", "data_fetching", "published"]);

export default function CreatedContentPane() {
  const { showToast, withPending, isPending } = useApp();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [platform, setPlatform] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = async () => {
    setError(null);
    try {
      setDrafts(await api.drafts(status || undefined, (platform || undefined) as any));
      setSelected(new Set());
    } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { load(); }, [status, platform]); // eslint-disable-line react-hooks/exhaustive-deps

  const deletable = useMemo(() => (drafts ?? []).filter((d) => !LOCKED.has(d.status)), [drafts]);
  const allChecked = deletable.length > 0 && deletable.every((d) => selected.has(d.content_id));
  const toggle = (cid: string) => setSelected((prev) => {
    const n = new Set(prev);
    n.has(cid) ? n.delete(cid) : n.add(cid);
    return n;
  });

  const remove = (contentIds: string[]) => withPending("content:del", async () => {
    if (!contentIds.length) return;
    const savedCount = (drafts ?? []).filter((d) => contentIds.includes(d.content_id) && d.status === "draft_saved").length;
    const warn = savedCount ? `\n其中 ${savedCount} 篇已存到平台草稿箱，系统不会去平台删除，请自行到创作者中心清理。` : "";
    if (!window.confirm(`删除 ${contentIds.length} 篇内容（文案、草稿记录、卡片图片）？不可恢复。${warn}`)) return;
    try {
      const r = await api.deleteContents(contentIds);
      const parts = [`已删除 ${r.deleted.length} 篇`];
      if (r.blocked.length) parts.push(`${r.blocked.length} 篇已发布不能删`);
      if (r.platform_drafts_left) parts.push(`请到平台草稿箱清理 ${r.platform_drafts_left} 篇`);
      showToast(parts.join("，"), r.blocked.length ? "info" : "success");
      await load();
    } catch (e: any) { showToast(`删除失败：${e.message}`, "error"); }
  });

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">▣</span>已生产内容</div>
          <div className="panel-sub">最近 100 条 · 已发布（有数据）的内容受保护不能删除</div>
        </div>
        <button className="btn sm" onClick={() => navigate("production", "review")}>去内容生产编辑 →</button>
      </div>

      <div className="toolbar" style={{ marginBottom: 10 }}>
        <select className="field" aria-label="状态" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v.text}</option>)}
        </select>
        <select className="field" aria-label="平台" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">全部平台</option>
          {["xhs", "douyin", "kuaishou", "wechat_channels"].map((p) => <option key={p} value={p}>{platformMeta(p as any).label}</option>)}
        </select>
        <button className="btn sm danger" style={{ marginLeft: "auto" }} disabled={!selected.size || isPending("content:del")}
          onClick={() => remove([...selected])}>删除所选（{selected.size}）</button>
      </div>

      {error && <div className="note" style={{ color: "var(--red)" }}>加载失败：{error} <button className="btn sm" onClick={load}>重试</button></div>}
      {drafts && drafts.length === 0 && <EmptyHint next="到「内容生产 → 生产线」运行一轮">暂无已生产内容</EmptyHint>}
      {drafts && drafts.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th style={{ width: 32 }}>
                <input type="checkbox" aria-label="全选可删除" checked={allChecked}
                  onChange={() => setSelected(allChecked ? new Set() : new Set(deletable.map((d) => d.content_id)))} />
              </th>
              <th>标题</th><th style={{ width: 70 }}>平台</th><th style={{ width: 60 }}>轮次</th>
              <th style={{ width: 110 }}>状态</th><th style={{ width: 140 }}>排期 / 存草稿</th><th style={{ width: 70 }}>操作</th>
            </tr></thead>
            <tbody>
              {drafts.map((d) => {
                const st = STATUS_LABEL[d.status] ?? { text: d.status, cls: "gray" };
                const locked = LOCKED.has(d.status);
                return (
                  <tr key={d.id}>
                    <td><input type="checkbox" aria-label={`选择 ${d.title ?? d.id}`} disabled={locked}
                      checked={selected.has(d.content_id)} onChange={() => toggle(d.content_id)} /></td>
                    <td>
                      <div className="ellipsis" style={{ maxWidth: 420, fontWeight: 550 }}>{d.title ?? "—"}</div>
                      {d.error_msg && <div className="ellipsis" style={{ fontSize: 11, color: "var(--orange)", maxWidth: 420 }}>{d.error_msg}</div>}
                    </td>
                    <td>{platformMeta(d.platform).label}</td>
                    <td>{d.round_id ? `#${d.round_id}` : "—"}</td>
                    <td><span className={`tag ${st.cls}`}>{st.text}</span></td>
                    <td style={{ fontSize: 11, color: "var(--muted)" }}>{d.scheduled_publish_at ?? d.saved_at ?? "—"}</td>
                    <td>
                      <button className="btn sm" disabled={locked} title={locked ? "已发布（有数据），不能删除" : "删除这篇内容"}
                        onClick={() => remove([d.content_id])}>删除</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
