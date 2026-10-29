import { useCallback, useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { api, MediaAsset, MediaUsageTarget } from "../api";

function fileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.onload = () => {
      const value = String(reader.result || "");
      const comma = value.indexOf(",");
      if (comma < 0) reject(new Error("图片编码失败"));
      else resolve(value.slice(comma + 1));
    };
    reader.readAsDataURL(file);
  });
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function MediaLibraryPane() {
  const { showToast, withPending, isPending } = useApp();
  const [items, setItems] = useState<MediaAsset[]>([]);
  const [targets, setTargets] = useState<MediaUsageTarget[]>([]);
  const [selectedContentId, setSelectedContentId] = useState("");
  const [query, setQuery] = useState("");
  const [filename, setFilename] = useState("");
  const [title, setTitle] = useState("");
  const [altText, setAltText] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [result, targetResult] = await Promise.all([api.mediaAssets(query), api.mediaUsageTargets()]);
      setItems(result.items);
      setTargets(targetResult.items);
      setSelectedContentId((current) => (
        current && targetResult.items.some((item) => item.id === current)
          ? current : targetResult.items[0]?.id ?? ""
      ));
    } catch (e) {
      const message = e instanceof Error ? e.message : "图片素材加载失败";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => { void load(); }, [load]);

  const chooseFile = (next: File | null) => {
    setFile(next);
    setFilename(next?.name || "");
    if (next && !title.trim()) setTitle(next.name.replace(/\.[^.]+$/, ""));
  };

  const upload = async () => {
    if (!file || isPending("media:upload")) return;
    if (file.size <= 0 || file.size > 12 * 1024 * 1024) {
      showToast("图片大小必须大于 0 且不超过 12MB", "error");
      return;
    }
    await withPending("media:upload", async () => {
      try {
        const data = await fileAsBase64(file);
        const result = await api.uploadMediaAsset({
          filename: filename || file.name,
          title: title.trim(),
          alt_text: altText.trim(),
          tags: tagsText.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean).slice(0, 30),
          data_base64: data,
        });
        showToast(result.duplicate ? "素材已存在，已复用现有记录" : "图片素材已入库", "success");
        setFile(null);
        setFilename("");
        setTitle("");
        setAltText("");
        setTagsText("");
        const input = document.getElementById("media-upload-input") as HTMLInputElement | null;
        if (input) input.value = "";
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "图片上传失败", "error");
      }
    });
  };

  const assignToContent = async (item: MediaAsset) => {
    if (!selectedContentId) {
      showToast("没有可选的待存草稿或平台草稿", "error");
      return;
    }
    await withPending("media:use:" + item.id, async () => {
      try {
        const result = await api.useMediaAsset(item.id, selectedContentId, "illustration");
        showToast(result.message, "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "图片分配失败", "error");
      }
    });
  };

  const archive = async (item: MediaAsset) => {
    await withPending(`media:archive:${item.id}`, async () => {
      try {
        await api.archiveMediaAsset(item.id);
        showToast("素材已归档", "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "归档失败", "error");
      }
    });
  };

  return (
    <div className="media-library">
      <section className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">图片素材库</div>
            <div className="panel-sub">图片按当前账号组独立存储；相同图片的重复上传会复用现有素材记录。</div>
          </div>
          <span className="tag gray">已入库 {items.length} 张</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
          <label className="field">
            <span className="field-label">选择图片</span>
            <input id="media-upload-input" type="file" accept="image/jpeg,image/png,image/webp,image/gif" onChange={(event) => chooseFile(event.target.files?.[0] ?? null)} />
          </label>
          <label className="field">
            <span className="field-label">素材名称</span>
            <input value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} placeholder="例如：品牌主视觉" />
          </label>
          <label className="field">
            <span className="field-label">替代文本</span>
            <input value={altText} maxLength={500} onChange={(event) => setAltText(event.target.value)} placeholder="描述图片内容，便于检索和无障碍访问" />
          </label>
          <label className="field">
            <span className="field-label">标签（逗号分隔）</span>
            <input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="封面, 品牌, 教程" />
          </label>
        </div>
        {file && <div className="panel-sub" style={{ marginTop: 8 }}>待上传：{file.name} · {formatBytes(file.size)} · 仅支持 JPEG、PNG、WebP、GIF</div>}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button className="btn sm" onClick={() => { setFile(null); setTitle(""); setAltText(""); setTagsText(""); setFilename(""); }}>清空</button>
          <button className="btn primary sm" onClick={() => void upload()} disabled={!file || isPending("media:upload")}>
            {isPending("media:upload") ? "上传中…" : "上传到素材库"}
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head" style={{ gap: 12, flexWrap: "wrap" }}>
          <div>
            <div className="panel-title">已入库素材</div>
            <div className="panel-sub">素材在本账号组内可复用，不会自动跨账号组共享。</div>
          </div>
          <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、标签或说明" />
            <button className="btn sm" onClick={() => void load()} disabled={loading}>刷新</button>
          </div>
        </div>
        {targets.length > 0 && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
            <span className="field-label">手动配图目标</span>
            <select value={selectedContentId} onChange={(event) => setSelectedContentId(event.target.value)} style={{ minWidth: 260, maxWidth: "100%" }}>
              {targets.map((target) => (
                <option value={target.id} key={target.id}>
                  {target.title} · {target.platform} · {target.draft_status} · {target.visual_status === "media_asset" ? "已有素材图" : target.visual_status === "generated" ? "AI 配图" : "模板降级图"} · #{target.id}
                </option>
              ))}
            </select>
            <span className="panel-sub">选择一篇尚未发布的内容，再点击素材卡片中的「配给此内容」。模板渐变图会明确标记为降级，正式配图需人工选择或后续 AI 配图。</span>
          </div>
        )}
        {error && <div className="panel-sub" style={{ color: "var(--red)" }}>{error}</div>}
        {loading ? <div className="empty-state">正在加载素材…</div> : items.length === 0 ? (
          <div className="empty-state">暂无图片素材。上传一张图片后，可在此查看和管理。</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))", gap: 14 }}>
            {items.map((item) => (
              <article key={item.id} style={{ border: "1px solid var(--border)", borderRadius: 10, overflow: "hidden", minWidth: 0 }}>
                <div style={{ aspectRatio: "4 / 3", background: "var(--surface-2)", display: "grid", placeItems: "center" }}>
                  <img src={item.file_url} alt={item.alt_text || item.title} loading="lazy" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
                </div>
                <div style={{ padding: 10 }}>
                  <div style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{item.title}</div>
                  <div className="panel-sub">{item.width} × {item.height} · {formatBytes(item.byte_size)}</div>
                  <div className="panel-sub" style={{ overflowWrap: "anywhere" }}>{item.original_filename}</div>
                  {item.tags.length > 0 && <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>{item.tags.map((tag) => <span className="tag gray" key={tag}>{tag}</span>)}</div>}
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 10 }}>
                    <a href={item.file_url} target="_blank" rel="noreferrer">查看原图</a>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <button className="btn sm primary" onClick={() => void assignToContent(item)} disabled={!selectedContentId || isPending("media:use:" + item.id)}>配给此内容</button>
                      <button className="btn sm" onClick={() => void archive(item)} disabled={isPending(`media:archive:${item.id}`)}>归档</button>
                    </div>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
