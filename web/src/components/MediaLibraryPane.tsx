import { useCallback, useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { api, MediaAsset, MediaUsageTarget, MediaGenerationSettings, MediaRecommendation } from "../api";

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
  const [usageRole, setUsageRole] = useState<"cover" | "illustration" | "background">("illustration");
  const [generationSettings, setGenerationSettings] = useState<MediaGenerationSettings | null>(null);
  const [generationPrompt, setGenerationPrompt] = useState("");
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("");
  const [altText, setAltText] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [purpose, setPurpose] = useState("门店环境");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [targetWarning, setTargetWarning] = useState("");
  const [recommendations, setRecommendations] = useState<MediaRecommendation[]>([]);
  const [recommendationMessage, setRecommendationMessage] = useState("");
  const [recommendationLoading, setRecommendationLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [result, targetResult, settingsResult] = await Promise.all([
        api.mediaAssets(query), api.mediaUsageTargets(), api.mediaGenerationSettings(),
      ]);
      setItems(result.items);
      setTargets(targetResult.items);
      setGenerationSettings(settingsResult);
      const requestedContentId = new URLSearchParams(window.location.search).get("focusContent");
      if (requestedContentId) {
        const requestedTarget = targetResult.items.find((item) => item.id === requestedContentId);
        if (requestedTarget) {
          setSelectedContentId(requestedContentId);
          setTargetWarning("");
        } else {
          setSelectedContentId("");
          setTargetWarning("从编辑页跳转的这篇内容当前不符合配图条件（可能已发布或不在待存草稿状态）。系统不会自动改选其他内容，请手动选择正确的待存草稿。");
        }
      } else {
        setTargetWarning("");
        setSelectedContentId((current) => (
          current && targetResult.items.some((item) => item.id === current)
            ? current : targetResult.items[0]?.id ?? ""
        ));
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "图片素材加载失败";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => { void load(); }, [load]);

  const chooseFiles = (next: File[]) => {
    setFiles(next);
    if (next.length === 1 && !title.trim()) setTitle(next[0].name.replace(/\.[^.]+$/, ""));
    if (next.length > 1) setTitle("");
  };

  const upload = async () => {
    if (!files.length || isPending("media:upload")) return;
    const invalid = files.filter((item) => item.size <= 0 || item.size > 12 * 1024 * 1024);
    if (invalid.length) {
      showToast(`有 ${invalid.length} 张图片大小不符合要求（单张需大于 0 且不超过 12MB），请移除后再上传`, "error");
      return;
    }
    await withPending("media:upload", async () => {
      let succeeded = 0;
      let duplicates = 0;
      let failed = 0;
      const failedFiles: File[] = [];
      const customTags = tagsText.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean);
      for (const item of files) {
        try {
          const data = await fileAsBase64(item);
          const result = await api.uploadMediaAsset({
            filename: item.name,
            title: (files.length === 1 && title.trim()) ? title.trim() : item.name.replace(/\.[^.]+$/, ""),
            alt_text: altText.trim(),
            tags: Array.from(new Set([purpose, ...customTags])).slice(0, 30),
            data_base64: data,
          });
          if (result.duplicate) duplicates += 1;
          else succeeded += 1;
        } catch (e) {
          failed += 1;
          failedFiles.push(item);
          console.error("media upload failed:", item.name, e);
        }
      }
      showToast(
        `批量导入完成：新增 ${succeeded} 张，重复复用 ${duplicates} 张，失败 ${failed} 张。${failed ? "失败项已保留，可直接重试。" : ""}`,
        failed ? "info" : "success",
      );
      setFiles(failedFiles);
      setTitle("");
      setAltText("");
      setTagsText("");
      const input = document.getElementById("media-upload-input") as HTMLInputElement | null;
      if (input) input.value = "";
      await load();
    });
  };

  const saveGenerationSettings = async () => {
    if (!generationSettings) return;
    await withPending("media:generation-settings", async () => {
      try {
        const saved = await api.saveMediaGenerationSettings({
          enabled: generationSettings.enabled,
          monthly_quota: generationSettings.monthly_quota,
          prompt_prefix: generationSettings.prompt_prefix,
          model: generationSettings.model,
          size: generationSettings.size,
        });
        setGenerationSettings(saved);
        showToast("AI 配图设置已保存", "success");
      } catch (e) {
        showToast(e instanceof Error ? e.message : "保存 AI 配图设置失败", "error");
      }
    });
  };

  const generate = async () => {
    if (!generationPrompt.trim()) {
      showToast("请先填写图片生成描述", "error");
      return;
    }
    await withPending("media:generate", async () => {
      try {
        const result = await api.generateMediaAsset({
          prompt: generationPrompt.trim(),
          title: title.trim(),
          alt_text: altText.trim(),
          tags: tagsText.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean).slice(0, 30),
        });
        showToast("AI 配图已生成，当前处于待人工审核状态", "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "AI 配图生成失败", "error");
      }
    });
  };

  const editImage = async (item: MediaAsset) => {
    if (!generationPrompt.trim()) {
      showToast("请先填写改图描述", "error");
      return;
    }
    await withPending("media:edit:" + item.id, async () => {
      try {
        await api.editMediaAsset(item.id, {
          prompt: generationPrompt.trim(),
          title: item.title + "（改图）",
          alt_text: altText.trim() || item.alt_text,
          tags: item.tags,
        });
        showToast("改图版本已生成，需人工审核后才能使用", "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "AI 改图失败", "error");
      }
    });
  };

  const review = async (item: MediaAsset, decision: "approved" | "rejected") => {
    await withPending("media:review:" + item.id, async () => {
      try {
        await api.reviewMediaAsset(item.id, decision, "人工审核");
        showToast(decision === "approved" ? "素材审核通过" : "素材已驳回", decision === "approved" ? "success" : "info");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "素材审核操作失败", "error");
      }
    });
  };

  const recommendForContent = async () => {
    if (!selectedContentId) { showToast("请先选择待配图内容", "error"); return; }
    setRecommendationLoading(true); setRecommendations([]); setRecommendationMessage("");
    try {
      const result = await api.recommendMediaAssets(selectedContentId, usageRole);
      setRecommendations(result.items); setRecommendationMessage(result.message);
      if (result.items.length === 0) showToast("没有找到明显匹配的已审核素材，可调整素材标签或继续手动选择", "info");
    } catch (e) { showToast(e instanceof Error ? e.message : "素材推荐失败", "error"); }
    finally { setRecommendationLoading(false); }
  };

  const assignToContent = async (item: MediaAsset) => {
    if (!selectedContentId) {
      showToast("没有可选的待存草稿或平台草稿", "error");
      return;
    }
    await withPending("media:use:" + item.id, async () => {
      try {
        const result = await api.useMediaAsset(item.id, selectedContentId, usageRole);
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
            <input id="media-upload-input" type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif" onChange={(event) => chooseFiles(Array.from(event.target.files ?? []))} />
          </label>
          <label className="field">
            <span className="field-label">用途分类（批量应用）</span>
            <select value={purpose} onChange={(event) => setPurpose(event.target.value)}>
              <option>封面</option><option>商品/菜品</option><option>门店环境</option><option>团队人物</option>
              <option>服务过程</option><option>活动海报</option><option>品牌标识</option><option>其他</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">素材名称（单张上传时使用）</span>
            <input value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} placeholder="批量导入时自动取文件名" />
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
        {files.length > 0 && <div className="panel-sub" style={{ marginTop: 8 }}>已选 {files.length} 张：{files.slice(0, 4).map((item) => `${item.name}（${formatBytes(item.size)}）`).join("、")}{files.length > 4 ? ` 等共 ${files.length} 张` : ""} · 单张不超过 12MB</div>}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button className="btn sm" onClick={() => { setFiles([]); setTitle(""); setAltText(""); setTagsText(""); const input = document.getElementById("media-upload-input") as HTMLInputElement | null; if (input) input.value = ""; }}>清空</button>
          <button className="btn primary sm" onClick={() => void upload()} disabled={!files.length || isPending("media:upload")}>
            {isPending("media:upload") ? `导入中 ${files.length} 张…` : `导入 ${files.length || ""} 张图片到素材库`}
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">AI 配图与改图</div>
            <div className="panel-sub">生成结果先进入待审核状态；只有人工审核通过的图片才能分配给内容。API 密钥只从服务端环境读取。</div>
          </div>
          <span className={generationSettings?.provider_configured ? "tag green" : "tag orange"}>
            {generationSettings?.provider_configured ? "已配置图片服务" : "未配置图片服务密钥"}
          </span>
        </div>
        {!generationSettings ? <div className="empty-state">正在加载 AI 配图设置…</div> : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
              <label className="field">
                <span className="field-label">启用 AI 配图</span>
                <select value={generationSettings.enabled ? "on" : "off"} onChange={(event) => setGenerationSettings({ ...generationSettings, enabled: event.target.value === "on" })}>
                  <option value="off">关闭</option><option value="on">启用</option>
                </select>
              </label>
              <label className="field">
                <span className="field-label">模型</span>
                <input value={generationSettings.model} maxLength={120} onChange={(event) => setGenerationSettings({ ...generationSettings, model: event.target.value })} />
              </label>
              <label className="field">
                <span className="field-label">图片尺寸</span>
                <select value={generationSettings.size} onChange={(event) => setGenerationSettings({ ...generationSettings, size: event.target.value as MediaGenerationSettings["size"] })}>
                  <option value="1024x1024">1024 × 1024</option>
                  <option value="1024x1536">1024 × 1536（竖图）</option>
                  <option value="1536x1024">1536 × 1024（横图）</option>
                </select>
              </label>
              <label className="field">
                <span className="field-label">本月生成额度</span>
                <input type="number" min={0} max={1000} value={generationSettings.monthly_quota} onChange={(event) => setGenerationSettings({ ...generationSettings, monthly_quota: Math.max(0, Math.min(1000, Number(event.target.value) || 0)) })} />
              </label>
            </div>
            <label className="field" style={{ display: "block", marginTop: 12 }}>
              <span className="field-label">统一视觉策略 / 提示词前缀</span>
              <textarea rows={3} maxLength={2000} value={generationSettings.prompt_prefix} onChange={(event) => setGenerationSettings({ ...generationSettings, prompt_prefix: event.target.value })} placeholder="例如：真实摄影风格、构图简洁、避免文字水印；根据账号品牌风格统一视觉表现" />
            </label>
            <div className="panel-sub" style={{ marginTop: 8 }}>
              本月已请求 {generationSettings.used_this_month} 次 / 配额 {generationSettings.monthly_quota} 次。服务地址：{generationSettings.api_base_url} · 密钥环境变量：{generationSettings.api_key_env}
            </div>
            <div className="panel-sub" style={{ marginTop: 4 }}>
              视觉初筛依据分辨率、构图、亮度、对比度、清晰度和颜色多样性给出可解释分数；它不判断图片是否符合文案语义，也不替代人工审核。
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
              <button className="btn sm" onClick={() => void saveGenerationSettings()} disabled={isPending("media:generation-settings")}>
                {isPending("media:generation-settings") ? "保存中…" : "保存 AI 配图设置"}
              </button>
            </div>
            <label className="field" style={{ display: "block", marginTop: 16 }}>
              <span className="field-label">本次生成 / 改图要求</span>
              <textarea rows={3} maxLength={2000} value={generationPrompt} onChange={(event) => setGenerationPrompt(event.target.value)} placeholder="描述画面主体、场景、构图、光线和需要避免的元素。改图时描述希望保留和修改的部分。" />
            </label>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
              <button className="btn primary sm" onClick={() => void generate()} disabled={!generationSettings.enabled || !generationSettings.provider_configured || !generationPrompt.trim() || isPending("media:generate")}>
                {isPending("media:generate") ? "生成中…" : "生成新图片"}
              </button>
            </div>
          </>
        )}
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
        {targetWarning && <div className="note" role="alert" style={{ color: "var(--orange)", marginBottom: 10 }}>{targetWarning}</div>}
        {targets.length > 0 && <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
          <button className="btn sm primary" onClick={() => void recommendForContent()} disabled={!selectedContentId || recommendationLoading}>
            {recommendationLoading ? "匹配中…" : "为当前内容推荐素材"}
          </button>
        </div>}
        {recommendations.length > 0 && <div className="panel" style={{ marginBottom: 14, padding: 12 }}>
          <div className="panel-title">匹配推荐（前 {recommendations.length} 项）</div>
          <div className="panel-sub">按内容标题、正文、素材名称及标签进行关键词匹配；仅展示已审核素材，不会自动配图。</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))", gap: 8, marginTop: 10 }}>
            {recommendations.map((rec) => {
              const asset = items.find((item) => item.id === rec.asset_id);
              if (!asset) return null;
              return <div key={rec.asset_id} style={{ display: "flex", gap: 10, border: "1px solid var(--border)", borderRadius: 8, padding: 8, minWidth: 0 }}>
                <img src={asset.file_url} alt={asset.alt_text || asset.title} loading="lazy" style={{ width: 68, height: 68, objectFit: "contain", background: "var(--surface-2)", borderRadius: 6, flexShrink: 0 }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{asset.title}</div>
                  <div className="panel-sub">{rec.reason || "关键词匹配"} · 匹配分 {rec.score}</div>
                  <button className="btn sm primary" style={{ marginTop: 6 }} onClick={() => void assignToContent(asset)} disabled={!selectedContentId || isPending("media:use:" + asset.id)}>使用此素材</button>
                </div>
              </div>;
            })}
          </div>
          {recommendationMessage && <div className="panel-sub" style={{ marginTop: 8 }}>{recommendationMessage}</div>}
        </div>}
        {targets.length > 0 && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
            <span className="field-label">手动配图目标</span>
            <select value={selectedContentId} onChange={(event) => { setSelectedContentId(event.target.value); setTargetWarning(""); }} style={{ minWidth: 260, maxWidth: "100%" }}>
              {targets.map((target) => (
                <option value={target.id} key={target.id}>
                  {target.title} · {target.platform} · {target.draft_status} · {target.visual_status === "media_asset" ? "已有素材图" : target.visual_status === "generated" ? "AI 配图" : "模板降级图"} · #{target.id}
                </option>
              ))}
            </select>
            <label className="field" style={{ minWidth: 140 }}>
              <span className="field-label">在内容中的用途</span>
              <select value={usageRole} onChange={(event) => setUsageRole(event.target.value as "cover" | "illustration" | "background")}>
                <option value="cover">封面</option><option value="illustration">正文配图</option><option value="background">背景</option>
              </select>
            </label>
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
                  <div className="panel-sub">
                    视觉初筛：{item.quality_score == null ? "未评分" : `${item.quality_score}/100`}
                    {item.quality_details?.reasons?.length ? ` · ${item.quality_details.reasons.slice(0, 2).join("；")}` : ""}
                  </div>
                  {item.tags.length > 0 && <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>{item.tags.map((tag) => <span className="tag gray" key={tag}>{tag}</span>)}</div>}
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 10 }}>
                    <a href={item.file_url} target="_blank" rel="noreferrer">查看原图</a>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <span className={item.review_status === "approved" ? "tag green" : item.review_status === "pending_review" ? "tag orange" : "tag gray"}>
                        {item.review_status === "approved" ? "审核通过" : item.review_status === "pending_review" ? "待审核" : "已驳回"}
                      </span>
                      {item.review_status === "pending_review" && (
                        <>
                          <button className="btn sm primary" onClick={() => void review(item, "approved")} disabled={isPending("media:review:" + item.id)}>通过审核</button>
                          <button className="btn sm" onClick={() => void review(item, "rejected")} disabled={isPending("media:review:" + item.id)}>驳回</button>
                        </>
                      )}
                      {item.review_status === "approved" && (
                        <>
                          <button className="btn sm primary" onClick={() => void assignToContent(item)} disabled={!selectedContentId || isPending("media:use:" + item.id)}>配给此内容</button>
                          <button className="btn sm" onClick={() => void editImage(item)} disabled={!generationSettings?.enabled || !generationSettings?.provider_configured || !generationPrompt.trim() || isPending("media:edit:" + item.id)}>AI 改图</button>
                        </>
                      )}
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
