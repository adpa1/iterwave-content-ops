/**
 * 选题池管理：本地选题库 + 热点转入 + 手动录入（与生产同一来源）。
 * 删除 = 软删除（不再参与评分与生产，可恢复）；手动/热点选题可彻底删除。
 */
import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "./EmptyHint";
import { api, PoolTopic, TopicPoolResponse } from "../api";

const SOURCE_LABEL: Record<string, { text: string; cls: string }> = {
  kb: { text: "本地库", cls: "gray" },
  hot: { text: "热点", cls: "orange" },
  manual: { text: "手动", cls: "blue" },
};
const PAGE = 20;

export default function TopicPoolPane() {
  const { showToast, withPending, isPending } = useApp();
  const [data, setData] = useState<TopicPoolResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [dimension, setDimension] = useState("");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState<"active" | "excluded">("active");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ title: "", dimension: "", summary: "", link: "" });

  const load = async () => {
    setError(null);
    try {
      setData(await api.topicPool({ q, dimension, source, status, limit: PAGE, offset: page * PAGE }));
      setSelected(new Set());
    } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { load(); }, [dimension, source, status, page]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (id: string) => setSelected((prev) => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });
  const items = data?.items ?? [];
  const allChecked = items.length > 0 && items.every((t) => selected.has(t.id));

  const remove = (ids: string[]) => withPending("topic:del", async () => {
    if (!ids.length) return;
    if (!window.confirm(`从选题池删除 ${ids.length} 个选题？删除后不再参与评分和生产，可在「已删除」里恢复。`)) return;
    try {
      const r = await api.deleteTopics(ids);
      showToast(`已删除 ${r.excluded.length} 个选题`, "success");
      await load();
    } catch (e: any) { showToast(`删除失败：${e.message}`, "error"); }
  });
  const restore = (ids: string[]) => withPending("topic:restore", async () => {
    try { await api.restoreTopics(ids); showToast(`已恢复 ${ids.length} 个选题`, "success"); await load(); }
    catch (e: any) { showToast(`恢复失败：${e.message}`, "error"); }
  });
  const purge = (ids: string[]) => withPending("topic:purge", async () => {
    if (!window.confirm("彻底删除后不可恢复（本地选题库里的选题和已产出过内容的选题会保留）。继续？")) return;
    try {
      const r = await api.purgeTopics(ids);
      showToast(`彻底删除 ${r.deleted.length} 个${r.kept.length ? `，${r.kept.length} 个保留（本地库或已产出内容）` : ""}`, "success");
      await load();
    } catch (e: any) { showToast(`删除失败：${e.message}`, "error"); }
  });
  const add = () => withPending("topic:add", async () => {
    try {
      const r = await api.createTopic(form);
      showToast(r.created ? "✓ 已加入选题池" : r.restored ? "已恢复该选题" : "选题池已有相同选题", "success");
      setForm({ title: "", dimension: "", summary: "", link: "" });
      setAdding(false);
      await load();
    } catch (e: any) { showToast(`添加失败：${e.message}`, "error"); }
  });

  const rep = data?.report;
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1;

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">☷</span>选题池</div>
          <div className="panel-sub">
            {rep ? `本地库 ${rep.kb_count} · 热点/手动 ${rep.extra_count} · 已删除 ${rep.excluded_count}` : "加载中…"}
            {rep?.kb_error && <span style={{ color: "var(--red)" }}> · 本地库读取失败：{rep.kb_error}</span>}
          </div>
        </div>
        <button className="btn sm primary" onClick={() => setAdding((v) => !v)}>{adding ? "收起" : "＋ 手动添加"}</button>
      </div>

      {adding && (
        <form className="toolbar" style={{ marginBottom: 12 }} onSubmit={(e) => { e.preventDefault(); add(); }}>
          <input className="field" style={{ flex: 2, minWidth: 200 }} placeholder="选题标题（≥4 字）" required
            value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <input className="field" style={{ width: 130 }} placeholder="内容方向" required list="dim-list"
            value={form.dimension} onChange={(e) => setForm({ ...form, dimension: e.target.value })} />
          <input className="field" style={{ flex: 2, minWidth: 160 }} placeholder="摘要（选填）"
            value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} />
          <input className="field" style={{ flex: 1, minWidth: 140 }} placeholder="原文链接（选填）"
            value={form.link} onChange={(e) => setForm({ ...form, link: e.target.value })} />
          <button type="submit" className="btn sm primary" disabled={isPending("topic:add")}>保存</button>
        </form>
      )}
      <datalist id="dim-list">{(data?.dimensions ?? []).map((d) => <option key={d} value={d} />)}</datalist>

      <div className="toolbar" style={{ marginBottom: 10 }}>
        <div className="tabs" role="tablist">
          {(["active", "excluded"] as const).map((s) => (
            <button key={s} role="tab" aria-selected={status === s} className={`tab ${status === s ? "active" : ""}`}
              onClick={() => { setStatus(s); setPage(0); }}>{s === "active" ? "可用" : "已删除"}</button>
          ))}
        </div>
        <form className="flex" style={{ gap: 6 }} onSubmit={(e) => { e.preventDefault(); setPage(0); load(); }}>
          <input className="field" style={{ width: 180 }} placeholder="搜索标题 / 摘要" value={q} onChange={(e) => setQ(e.target.value)} />
          <button type="submit" className="btn sm">搜索</button>
        </form>
        <select className="field" aria-label="内容方向" value={dimension} onChange={(e) => { setDimension(e.target.value); setPage(0); }}>
          <option value="">全部方向</option>
          {(data?.dimensions ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select className="field" aria-label="来源" value={source} onChange={(e) => { setSource(e.target.value); setPage(0); }}>
          <option value="">全部来源</option>
          <option value="kb">本地库</option>
          <option value="hot">热点</option>
          <option value="manual">手动</option>
        </select>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {status === "active" ? (
            <button className="btn sm danger" disabled={!selected.size || isPending("topic:del")} onClick={() => remove([...selected])}>
              删除所选（{selected.size}）
            </button>
          ) : (
            <>
              <button className="btn sm" disabled={!selected.size} onClick={() => restore([...selected])}>恢复所选（{selected.size}）</button>
              <button className="btn sm danger" disabled={!selected.size} onClick={() => purge([...selected])}>彻底删除</button>
            </>
          )}
        </div>
      </div>

      {error && <div className="note" style={{ color: "var(--red)" }}>加载失败：{error} <button className="btn sm" onClick={load}>重试</button></div>}
      {data && items.length === 0 && (
        status === "active"
          ? <EmptyHint next="检查「系统设置」里的本地选题库路径，或在「热点雷达」把热点转入选题库，也可以点右上角手动添加">选题池为空</EmptyHint>
          : <EmptyHint next="在「可用」列表里删除的选题会出现在这里，可随时恢复">没有已删除的选题</EmptyHint>
      )}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th style={{ width: 32 }}>
                <input type="checkbox" aria-label="全选" checked={allChecked}
                  onChange={() => setSelected(allChecked ? new Set() : new Set(items.map((t) => t.id)))} />
              </th>
              <th>选题</th><th style={{ width: 110 }}>方向</th><th style={{ width: 70 }}>来源</th>
              <th style={{ width: 70 }}>已产出</th><th style={{ width: 96 }}>日期</th><th style={{ width: 70 }}>操作</th>
            </tr></thead>
            <tbody>
              {items.map((t: PoolTopic) => (
                <tr key={t.id}>
                  <td><input type="checkbox" aria-label={`选择 ${t.title}`} checked={selected.has(t.id)} onChange={() => toggle(t.id)} /></td>
                  <td>
                    <div style={{ fontWeight: 550 }}>
                      {t.link ? <a href={t.link} target="_blank" rel="noreferrer">{t.title}</a> : t.title}
                    </div>
                    {(t.summary || t.excluded_reason) && (
                      <div className="ellipsis" style={{ fontSize: 11, color: "var(--muted)", maxWidth: 520 }}>
                        {t.excluded ? `删除原因：${t.excluded_reason ?? "—"}` : t.summary}
                      </div>
                    )}
                  </td>
                  <td><span className="tag gray">{t.dimension || "未分类"}</span></td>
                  <td><span className={`tag ${SOURCE_LABEL[t.source_kind].cls}`}>{SOURCE_LABEL[t.source_kind].text}</span></td>
                  <td>{t.content_count || "—"}</td>
                  <td style={{ fontSize: 11, color: "var(--muted)" }}>{t.date ?? "—"}</td>
                  <td>
                    {t.excluded
                      ? <button className="btn sm" onClick={() => restore([t.id])}>恢复</button>
                      : <button className="btn sm" onClick={() => remove([t.id])}>删除</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.total > PAGE && (
        <div className="flex" style={{ gap: 8, justifyContent: "flex-end", alignItems: "center", marginTop: 10, fontSize: 12 }}>
          <span style={{ color: "var(--muted)" }}>共 {data.total} 条 · 第 {page + 1}/{pages} 页</span>
          <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
          <button className="btn sm" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>下一页</button>
        </div>
      )}
    </div>
  );
}
