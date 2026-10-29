import { useEffect, useMemo, useRef, useState } from "react";
import { api, type KbPreview, type KbSource } from "../api";
import { useApp } from "../AppContext";
import EmptyHint from "./EmptyHint";

const FIELD_LABELS: Record<string, string> = {
  source: "来源",
  title: "标题",
  summary: "摘要",
  link: "链接",
  date: "日期",
  dimension: "维度",
};

const KIND_LABELS: Record<string, string> = { file: "上传", server: "服务器路径", remote: "远程地址" };

/** 选题库三种来源：上传 / 远程（CSV·JSON·RSS）/ 服务器路径 + 字段映射下拉 + 5 行预览 */
export default function KbSourcesPanel() {
  const { showToast } = useApp();
  const [sources, setSources] = useState<KbSource[]>([]);
  const [maxMb, setMaxMb] = useState(20);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [addMode, setAddMode] = useState<"upload" | "server" | "remote">("upload");
  const [busy, setBusy] = useState("");
  const [preview, setPreview] = useState<KbPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const fileRef = useRef<HTMLInputElement>(null);

  const [uploadName, setUploadName] = useState("");
  const [serverPath, setServerPath] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [remoteName, setRemoteName] = useState("");
  const [remoteFormat, setRemoteFormat] = useState("auto");
  const [remoteHeaders, setRemoteHeaders] = useState("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.kbSources();
      setSources(res.items);
      setMaxMb(res.max_upload_mb);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const readFile = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsDataURL(file);
    });

  const submitUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) {
      showToast("先选择一个文件", "error");
      return;
    }
    if (file.size > maxMb * 1024 * 1024) {
      showToast(`文件超过 ${maxMb}MB 上限`, "error");
      return;
    }
    setBusy("upload");
    try {
      const content = await readFile(file);
      await api.uploadKbSource({ filename: file.name, content, name: uploadName.trim() || undefined });
      showToast("上传成功，已加入选题库来源", "success");
      if (fileRef.current) fileRef.current.value = "";
      setUploadName("");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const submitServer = async () => {
    if (!serverPath.trim()) return;
    setBusy("server");
    try {
      await api.addKbServerSource({ path: serverPath.trim() });
      setServerPath("");
      showToast("服务器路径已加入来源", "success");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const parseHeaders = (text: string): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const idx = line.indexOf(":");
      if (idx <= 0) continue;
      const key = line.slice(0, idx).trim();
      const value = line.slice(idx + 1).trim();
      if (key && value) out[key] = value;
    }
    return out;
  };

  const submitRemote = async () => {
    if (!remoteUrl.trim()) return;
    setBusy("remote");
    try {
      await api.addKbRemoteSource({
        url: remoteUrl.trim(),
        name: remoteName.trim() || undefined,
        format: remoteFormat,
        headers: parseHeaders(remoteHeaders),
      });
      setRemoteUrl("");
      setRemoteName("");
      setRemoteHeaders("");
      showToast("远程来源已接入，之后每天自动同步", "success");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const importToPool = async () => {
    setBusy("import");
    try {
      const res = await api.importKbToPool();
      showToast(`已并入选题池：新增 ${res.inserted} 条，当前共 ${res.total} 条`, "success");
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const openPreview = async (source: KbSource) => {
    setBusy(source.id);
    try {
      const res = await api.previewKbSource(source.id);
      setPreview(res);
      setMapping(res.mapping);
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const saveMapping = async () => {
    if (!preview) return;
    if (!mapping.title) {
      showToast("至少要为「标题」选择一个列", "error");
      return;
    }
    setBusy("mapping");
    try {
      await api.updateKbSource(preview.id, { field_mapping: mapping });
      showToast("字段映射已保存", "success");
      setPreview(null);
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const toggle = async (source: KbSource) => {
    setBusy(source.id);
    try {
      await api.updateKbSource(source.id, { enabled: !source.enabled });
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const remove = async (source: KbSource) => {
    if (!window.confirm(`删除来源「${source.name}」？上传文件会一并删除。`)) return;
    setBusy(source.id);
    try {
      await api.deleteKbSource(source.id);
      showToast("来源已删除", "success");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const sync = async (source: KbSource) => {
    setBusy(source.id);
    try {
      const res = await api.syncKbSource(source.id);
      showToast(`同步完成，共 ${res.rows} 行`, "success");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const addEmpty = async () => {
    setBusy("default");
    try {
      await api.addKbDefaultSource();
      showToast("已创建空库，可直接手动添加选题", "success");
      await load();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const columns = preview?.columns || [];
  const fieldOptions = useMemo(
    () => columns.map((c) => ({ value: c, label: c })),
    [columns],
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">⚙</span>选题库来源</div>
          <div className="panel-sub">
            上传文件、远程地址或服务器路径都可以；不接选题库也能只用热点与手动选题
          </div>
        </div>
        <div className="head-actions">
          <button className="btn sm primary" type="button" onClick={importToPool} disabled={busy === "import"}>
            {busy === "import" ? "导入中…" : "⇣ 导入到选题池"}
          </button>
          <button className="btn sm" type="button" onClick={addEmpty} disabled={busy === "default"}>建空库</button>
          <button className="btn sm" type="button" onClick={load} disabled={loading}>↻ 刷新</button>
        </div>
      </div>

      {error && <div className="note" style={{ color: "var(--red)" }}>加载失败：{error}</div>}

      {loading ? (
        <div className="note">加载来源中…</div>
      ) : sources.length === 0 ? (
        <EmptyHint next="上传一个 CSV / Excel，或接入远程 CSV / JSON / RSS">还没有选题库来源</EmptyHint>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>来源</th><th>类型</th><th>位置</th><th>行数</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id}>
                  <td style={{ fontWeight: 600 }}>{s.name}</td>
                  <td><span className="tag gray">{KIND_LABELS[s.kind] ?? s.kind}</span></td>
                  <td style={{ fontSize: 11, color: "var(--muted)" }}>{s.display_path}</td>
                  <td>{s.rows == null ? "—" : s.rows}</td>
                  <td>
                    {s.last_error
                      ? <span className="tag red" title={s.last_error}>同步失败</span>
                      : s.enabled
                        ? <span className="tag green">启用</span>
                        : <span className="tag gray">停用</span>}
                  </td>
                  <td>
                    <div className="flex wrap" style={{ gap: 6 }}>
                      <button className="btn xs" type="button" onClick={() => openPreview(s)} disabled={busy === s.id}>映射与预览</button>
                      {s.kind === "remote" && (
                        <button className="btn xs soft" type="button" onClick={() => sync(s)} disabled={busy === s.id}>立即同步</button>
                      )}
                      <button className="btn xs soft" type="button" onClick={() => toggle(s)} disabled={busy === s.id}>
                        {s.enabled ? "停用" : "启用"}
                      </button>
                      <button className="btn xs danger" type="button" onClick={() => remove(s)} disabled={busy === s.id}>删除</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {preview && (
        <div className="panel" style={{ marginTop: 16, background: "var(--bg-soft, #f8fafc)" }}>
          <div className="panel-head">
            <div>
              <div className="panel-title">字段映射 · {preview.display_path}</div>
              <div className="panel-sub">共 {preview.rows} 行；没有维度列时归入默认维度「综合」</div>
            </div>
            <div className="head-actions">
              <button className="btn sm soft" type="button" onClick={() => setPreview(null)}>收起</button>
              <button className="btn sm" type="button" onClick={saveMapping} disabled={busy === "mapping"}>保存映射</button>
            </div>
          </div>

          <div className="grid two" style={{ gap: 12 }}>
            {Object.keys(FIELD_LABELS).map((field) => (
              <div key={field}>
                <div className="field-label">{FIELD_LABELS[field]}{field === "title" ? "（必填）" : ""}</div>
                <select
                  className="field"
                  value={mapping[field] || ""}
                  onChange={(e) => setMapping({ ...mapping, [field]: e.target.value })}
                >
                  <option value="">— 不映射 —</option>
                  {fieldOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
            ))}
          </div>

          {preview.sample.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {preview.sample.map((row, i) => (
                    <tr key={i}>{columns.map((c) => <td key={c} style={{ fontSize: 11 }}>{row[c] || "—"}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title">添加来源</div>
            <div className="panel-sub">上传最大 {maxMb}MB；远程来源每天 7:00 自动同步</div>
          </div>
          <div className="tabs">
            {(["upload", "server", "remote"] as const).map((m) => (
              <button key={m} className={`tab ${addMode === m ? "active" : ""}`} onClick={() => setAddMode(m)}>
                {m === "upload" ? "上传文件" : m === "server" ? "服务器路径" : "远程地址"}
              </button>
            ))}
          </div>
        </div>

        {addMode === "upload" && (
          <div className="flex wrap" style={{ gap: 8, alignItems: "center" }}>
            <input ref={fileRef} type="file" accept=".csv,.json,.jsonl,.ndjson,.xlsx,.db,.sqlite,.sqlite3" />
            <input className="field" style={{ width: 200 }} placeholder="来源名称（可选）"
              value={uploadName} onChange={(e) => setUploadName(e.target.value)} />
            <button className="btn sm" type="button" onClick={submitUpload} disabled={busy === "upload"}>
              {busy === "upload" ? "上传中…" : "上传"}
            </button>
          </div>
        )}

        {addMode === "server" && (
          <div className="flex wrap" style={{ gap: 8, alignItems: "center" }}>
            <input className="field" style={{ flex: 1, minWidth: 240 }} placeholder="相对数据目录或绝对路径，例如 kb/topics.csv"
              value={serverPath} onChange={(e) => setServerPath(e.target.value)} />
            <button className="btn sm" type="button" onClick={submitServer} disabled={busy === "server"}>添加</button>
          </div>
        )}

        {addMode === "remote" && (
          <div style={{ display: "grid", gap: 8 }}>
            <div className="flex wrap" style={{ gap: 8 }}>
              <input className="field" style={{ flex: 1, minWidth: 240 }} placeholder="https://example.com/topics.csv 或 /feed.xml"
                value={remoteUrl} onChange={(e) => setRemoteUrl(e.target.value)} />
              <input className="field" style={{ width: 160 }} placeholder="来源名称（可选）"
                value={remoteName} onChange={(e) => setRemoteName(e.target.value)} />
              <select className="field" style={{ width: 110 }} value={remoteFormat} onChange={(e) => setRemoteFormat(e.target.value)}>
                <option value="auto">自动识别</option>
                <option value="csv">CSV</option>
                <option value="json">JSON</option>
                <option value="rss">RSS</option>
              </select>
            </div>
            <textarea className="field" rows={3} placeholder="鉴权请求头（可选），每行 Key: Value"
              value={remoteHeaders} onChange={(e) => setRemoteHeaders(e.target.value)} />
            <div>
              <button className="btn sm" type="button" onClick={submitRemote} disabled={busy === "remote"}>
                {busy === "remote" ? "连接中…" : "接入并同步"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
