import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

function tagLabel(tag: string): string {
  return PLATFORM_CODES.has(tag) ? platformMeta(tag).label : tag;
}

function splitTags(text: string): string[] {
  return text.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean);
}

function assetState(item: MediaAsset): Filter {
  if (item.status === "archived") return "archived";
  if (item.review_status === "pending_review") return "pending";
  if (item.review_status === "rejected") return "rejected";
  return "approved";
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
  const [recommendationContentId, setRecommendationContentId] = useState("");
  const [recommendationRole, setRecommendationRole] = useState<"cover" | "illustration" | "background">("cover");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [result, targetResult, settingsResult] = await Promise.all([
        api.mediaAssets(query, "all"), api.mediaUsageTargets(), api.mediaGenerationSettings(),
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
      setError(e instanceof Error ? e.message : "图片素材加载失败");
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void load(); }, 250);
    return () => window.clearTimeout(timer);
  }, [load]);

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

  // ---------- AI 设置 ----------
  const saveSettings = async () => {
    if (!settings) return;
    await withPending("media:generation-settings", async () => {
      try {
        const saved = await api.saveMediaGenerationSettings({
          enabled: settings.enabled, monthly_quota: settings.monthly_quota,
          prompt_prefix: settings.prompt_prefix, model: settings.model, size: settings.size,
        });
        setSettings(saved);
        showToast("AI 配图设置已保存", "success");
      } catch (e) {
        showToast(e instanceof Error ? e.message : "保存失败", "error");
      }
    });
  };

  // ---------- AI 生成 / 改图 ----------
  const startEdit = (item: MediaAsset) => {
    setEditTarget(item);
    setPanel("generate");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const runGenerate = async () => {
    if (!prompt.trim()) { showToast(editTarget ? "请描述要怎么改" : "请描述想要的画面", "error"); return; }
    const key = editTarget ? `media:edit:${editTarget.id}` : "media:generate";
    await withPending(key, async () => {
      try {
        if (editTarget) {
          await api.editMediaAsset(editTarget.id, {
            prompt: prompt.trim(), title: genTitle.trim() || `${editTarget.title}（改图）`,
            alt_text: editTarget.alt_text, tags: editTarget.tags,
          });
        } else {
          await api.generateMediaAsset({ prompt: prompt.trim(), title: genTitle.trim(), tags: [] });
        }
        showToast("已生成，图片在「待审核」里，通过后才能使用", "success");
        setPrompt(""); setGenTitle(""); setEditTarget(null); setFilter("pending");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "生成失败", "error");
      }
    });
  };

  // ---------- 审核 / 分配 / 归档 ----------
  const review = async (item: MediaAsset, decision: "approved" | "rejected") => {
    await withPending("media:review:" + item.id, async () => {
      try {
        await api.reviewMediaAsset(item.id, decision, "人工审核");
        showToast(decision === "approved" ? "已通过，可以配给内容了" : "已驳回", decision === "approved" ? "success" : "info");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "审核操作失败", "error");
      }
    });
  };

  const recommendForContent = async () => {
    if (!selectedContentId) { showToast("请先选择待配图内容", "error"); return; }
    setRecommendationLoading(true); setRecommendations([]); setRecommendationMessage("");
    try {
      const result = await api.recommendMediaAssets(selectedContentId, usageRole);
      setRecommendations(result.items); setRecommendationMessage(result.message);
      setRecommendationContentId(selectedContentId); setRecommendationRole(usageRole);
      if (result.items.length === 0) showToast("没有找到明显匹配的已审核素材，可调整素材标签或继续手动选择", "info");
    } catch (e) { showToast(e instanceof Error ? e.message : "素材推荐失败", "error"); }
    finally { setRecommendationLoading(false); }
  };

  const assignToContent = async (item: MediaAsset) => {
    if (!selectedContentId) { showToast("请先在上方选择要配图的内容", "error"); return; }
    await withPending("media:use:" + item.id, async () => {
      try {
        const result = await api.useMediaAsset(item.id, selectedContentId, usageRole);
        showToast(result.message, "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "配图失败", "error");
      }
    });
  };

  const archive = async (item: MediaAsset) => {
    if (!window.confirm(`归档「${item.title}」？归档后不会出现在推荐和配图里。`)) return;
    await withPending(`media:archive:${item.id}`, async () => {
      try {
        await api.archiveMediaAsset(item.id);
        showToast("已归档", "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "归档失败", "error");
      }
    });
  };

  const recommend = async () => {
    if (!selectedContentId) return;
    setRecommendationLoading(true); setRecommendations([]); setRecommendationMessage("");
    try {
      const result = await api.recommendMediaAssets(selectedContentId, usageRole);
      setRecommendations(result.items);
      setRecommendationMessage(result.method === "vision_model" ? "按画面与文案的语义相关度排序" : "按标题、标签与文案的关键词匹配排序");
      setRecommendationKey(`${selectedContentId}:${usageRole}`);
      if (!result.items.length) showToast("没有找到匹配的已审核素材，可以补充素材标签，或直接按内容生成", "info");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "推荐失败", "error");
    } finally {
      setRecommendationLoading(false);
    }
  };

  const generateForContent = async () => {
    if (!selectedContentId) return;
    await withPending("media:generate-for-content", async () => {
      try {
        await api.generateMediaForContent(selectedContentId, usageRole);
        showToast("候选配图已生成，在「待审核」里确认后即可配给这篇内容", "success");
        setFilter("pending");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "生成失败", "error");
      }
    });
  };

  const showRecommendations = recommendations.length > 0 && recommendationKey === `${selectedContentId}:${usageRole}`;

  return (
    <div className="ml">
      {/* 顶部：标题、统计与主要操作 */}
      <section className="panel ml-head">
        <div className="ml-head-row">
          <div className="ml-head-title">
            <div className="panel-title">图片素材库</div>
            <div className="ml-stats">
              <span>可用 <b>{counts.approved}</b></span>
              <span className={counts.pending ? "warn" : ""}>待审核 <b>{counts.pending}</b></span>
              {settings && <span>本月 AI 额度剩余 <b>{quotaLeft}</b>/{settings.monthly_quota}</span>}
            </div>
          </div>
          <div className="ml-actions">
            <button className={`btn sm ${panel === "settings" ? "soft" : ""}`} onClick={() => togglePanel("settings")}>AI 设置</button>
            <button className={`btn sm ${panel === "generate" ? "soft" : ""}`} onClick={() => { setEditTarget(null); togglePanel("generate"); }}>AI 生成</button>
            <button className="btn primary sm" onClick={() => togglePanel("upload")}>{panel === "upload" ? "收起上传" : "上传图片"}</button>
          </div>
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
              本月成功生成 {generationSettings.used_this_month} 次 / 配额 {generationSettings.monthly_quota} 次。失败的生成不计入已用额度。
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
        {recommendations.length > 0 && recommendationContentId === selectedContentId && recommendationRole === usageRole && <div className="panel" style={{ marginBottom: 14, padding: 12 }}>
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
            <select value={selectedContentId} onChange={(event) => { setSelectedContentId(event.target.value); setTargetWarning(""); setRecommendations([]); setRecommendationMessage(""); }} style={{ minWidth: 260, maxWidth: "100%" }}>
              {targets.map((target) => (
                <option value={target.id} key={target.id}>
                  {target.title} · {({ xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号" } as Record<string, string>)[target.platform] ?? "平台内容"} · {target.draft_status === "draft_saved" ? "已存平台草稿" : "待存草稿"} · {target.visual_status === "media_asset" ? "已有素材图" : target.visual_status === "generated" ? "AI 配图" : "默认配图"}
                </option>
              ))}
            </select>
            <label className="field" style={{ minWidth: 140 }}>
              <span className="field-label">在内容中的用途</span>
              <select value={usageRole} onChange={(event) => { setUsageRole(event.target.value as "cover" | "illustration" | "background"); setRecommendations([]); setRecommendationMessage(""); }}>
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
                </>
              )}
            </div>
            <div className="ml-form">
              <label className="ml-field">
                <span>用途</span>
                <select value={purpose} onChange={(e) => setPurpose(e.target.value)}>
                  {PURPOSES.map((p) => <option key={p}>{p}</option>)}
                </select>
              </label>
              <label className="ml-field">
                <span>标签 <em>逗号分隔，方便推荐时匹配</em></span>
                <input value={tagsText} onChange={(e) => setTagsText(e.target.value)} placeholder="如：秋季, 露营, 户外" />
              </label>
              <label className="ml-field">
                <span>名称 <em>{files.length > 1 ? "多张时自动用文件名" : "默认取文件名"}</em></span>
                <input value={title} maxLength={200} disabled={files.length > 1} onChange={(e) => setTitle(e.target.value)} placeholder="素材名称" />
              </label>
              <label className="ml-field">
                <span>图片描述 <em>可选</em></span>
                <input value={altText} maxLength={500} onChange={(e) => setAltText(e.target.value)} placeholder="画面里有什么，便于检索" />
              </label>
            </div>
            <div className="ml-drawer-foot">
              <button className="btn sm ghost" onClick={resetUpload} disabled={!files.length}>清空</button>
              <button className="btn primary sm" onClick={() => void upload()} disabled={!files.length || isPending("media:upload")}>
                {isPending("media:upload") ? "上传中…" : files.length ? `上传 ${files.length} 张` : "上传"}
              </button>
            </div>
          </div>
        )}

        {panel === "generate" && (
          <div className="ml-drawer">
            {editTarget && (
              <div className="ml-edit-target">
                <img src={editTarget.file_url} alt="" />
                <div>基于「{editTarget.title}」改图 <button className="btn sm ghost" onClick={() => setEditTarget(null)}>改为新生成</button></div>
              </div>
            )}
            {!aiReady && <div className="note orange">{aiBlockedReason}</div>}
            <label className="ml-field wide">
              <span>{editTarget ? "要怎么改" : "想要什么画面"}</span>
              <textarea rows={4} maxLength={2000} value={prompt} onChange={(e) => setPrompt(e.target.value)}
                placeholder={editTarget ? "说明保留什么、改什么，例如：保留人物和构图，背景换成秋天的山林" : "主体、场景、构图、光线，以及要避免的元素，例如：山间草地上的帐篷，清晨侧光，不要文字"} />
            </label>
            <div className="ml-form">
              <label className="ml-field">
                <span>名称 <em>可选</em></span>
                <input value={genTitle} maxLength={200} onChange={(e) => setGenTitle(e.target.value)} placeholder="不填则自动命名" />
              </label>
            </div>
            <div className="ml-drawer-foot">
              <span className="ml-hint">生成的图片先进「待审核」，通过后才能使用；失败不会显示为成功</span>
              <button className="btn primary sm" onClick={() => void runGenerate()}
                disabled={!aiReady || !prompt.trim() || isPending(editTarget ? `media:edit:${editTarget.id}` : "media:generate")}>
                {isPending(editTarget ? `media:edit:${editTarget.id}` : "media:generate") ? "生成中…" : editTarget ? "生成改图" : "生成图片"}
              </button>
            </div>
          </div>
        )}

        {panel === "settings" && settings && (
          <div className="ml-drawer">
            <div className="ml-service">
              <span className={settings.provider_configured ? "tag green" : "tag orange"}>
                {settings.provider_configured ? "图片服务已接入" : "图片服务未接入"}
              </span>
              <span className="ml-hint">{settings.provider_configured ? "密钥由服务器保管，页面不显示" : "需要管理员在服务器配置图片服务密钥"}</span>
            </div>
            <div className="ml-form">
              <label className="ml-field">
                <span>AI 配图</span>
                <select value={settings.enabled ? "on" : "off"} onChange={(e) => setSettings({ ...settings, enabled: e.target.value === "on" })}>
                  <option value="off">关闭</option><option value="on">开启</option>
                </select>
              </label>
              <label className="ml-field">
                <span>默认尺寸</span>
                <select value={settings.size} onChange={(e) => setSettings({ ...settings, size: e.target.value as MediaGenerationSettings["size"] })}>
                  <option value="1024x1024">方图 1024 × 1024</option>
                  <option value="1024x1536">竖图 1024 × 1536</option>
                  <option value="1536x1024">横图 1536 × 1024</option>
                </select>
              </label>
              <label className="ml-field">
                <span>每月额度 <em>本月已成功生成 {settings.used_this_month} 次，失败不计</em></span>
                <input type="number" min={0} max={1000} value={settings.monthly_quota}
                  onChange={(e) => setSettings({ ...settings, monthly_quota: Math.max(0, Math.min(1000, Number(e.target.value) || 0)) })} />
              </label>
              <label className="ml-field">
                <span>模型</span>
                <input value={settings.model} maxLength={120} onChange={(e) => setSettings({ ...settings, model: e.target.value })} />
              </label>
            </div>
            <label className="ml-field wide">
              <span>统一画面风格 <em>会加在每次生成的描述前面</em></span>
              <textarea rows={3} maxLength={2000} value={settings.prompt_prefix} onChange={(e) => setSettings({ ...settings, prompt_prefix: e.target.value })}
                placeholder="例如：真实摄影风格，自然光，构图简洁，不出现文字和水印" />
            </label>
            <div className="ml-drawer-foot">
              <button className="btn primary sm" onClick={() => void saveSettings()} disabled={isPending("media:generation-settings")}>
                {isPending("media:generation-settings") ? "保存中…" : "保存设置"}
              </button>
            </div>
          </div>
        )}
      </section>

      {/* 为内容配图 */}
      <section className="panel ml-assign">
        <div className="ml-assign-row">
          <div className="ml-assign-label">为内容配图</div>
          {targets.length === 0 ? (
            <span className="ml-hint">目前没有待配图的草稿。生成内容后，可以在这里给它挑图或生成配图。</span>
          ) : (
            <>
              <select className="ml-select" value={selectedContentId} onChange={(e) => { setSelectedContentId(e.target.value); setTargetWarning(""); }}>
                {!selectedContentId && <option value="">请选择内容</option>}
                {targets.map((t) => (
                  <option value={t.id} key={t.id}>
                    {t.title || "未命名内容"} · {platformMeta(t.platform).label} · {DRAFT_STATUS_LABEL[t.draft_status] ?? "草稿"}
                  </option>
                ))}
              </select>
              <div className="ml-seg" role="group" aria-label="图片用途">
                {(Object.keys(ROLE_LABEL) as UsageRole[]).map((role) => (
                  <button key={role} className={usageRole === role ? "on" : ""} onClick={() => setUsageRole(role)}>{ROLE_LABEL[role]}</button>
                ))}
              </div>
              <button className="btn sm" onClick={() => void recommend()} disabled={!selectedContentId || recommendationLoading}>
                {recommendationLoading ? "匹配中…" : "推荐素材"}
              </button>
              <button className="btn sm primary" onClick={() => void generateForContent()}
                title={aiReady ? "" : aiBlockedReason}
                disabled={!selectedContentId || !aiReady || isPending("media:generate-for-content")}>
                {isPending("media:generate-for-content") ? "生成中…" : "按内容生成配图"}
              </button>
            </>
          )}
        </div>
        {selectedTarget && (
          <div className="ml-hint ml-target-state">
            当前配图：{VISUAL_LABEL[selectedTarget.visual_status] ?? "未知"}。在下方素材卡片点「配给这篇」即可替换为{ROLE_LABEL[usageRole]}，不会自动替换。
          </div>
        )}
        {targetWarning && <div className="note orange" role="alert">{targetWarning}</div>}
        {showRecommendations && (
          <div className="ml-recs">
            <div className="ml-recs-head"><b>推荐 {recommendations.length} 张</b><span className="ml-hint">{recommendationMessage}，仅供参考</span></div>
            <div className="ml-recs-list">
              {recommendations.map((rec) => {
                const asset = items.find((item) => item.id === rec.asset_id);
                if (!asset) return null;
                return (
                  <div className="ml-rec" key={rec.asset_id}>
                    <img src={asset.file_url} alt={asset.alt_text || asset.title} loading="lazy" />
                    <div className="ml-rec-body">
                      <div className="ml-card-title" title={asset.title}>{asset.title}</div>
                      <div className="ml-hint ml-clamp">{rec.reason || "关键词匹配"}</div>
                      <button className="btn sm primary" onClick={() => void assignToContent(asset)} disabled={isPending("media:use:" + asset.id)}>配给这篇</button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </section>

      {/* 素材列表 */}
      <section className="panel">
        <div className="ml-toolbar">
          <div className="ml-filters" role="tablist">
            {([["all", "全部"], ["pending", "待审核"], ["approved", "可用"], ["rejected", "已驳回"], ["archived", "已归档"]] as [Filter, string][]).map(([key, label]) => (
              <button key={key} role="tab" aria-selected={filter === key} className={filter === key ? "on" : ""} onClick={() => setFilter(key)}>
                {label}<span className={key === "pending" && counts.pending ? "badge warn" : "badge"}>{counts[key]}</span>
              </button>
            ))}
          </div>
          <input className="ml-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索名称、标签、描述" />
        </div>

        {error && <div className="note orange" role="alert">{error}</div>}
        {loading && items.length === 0 ? <div className="empty">正在加载素材…</div> : visible.length === 0 ? (
          <div className="ml-empty">
            {filter === "pending" ? "没有待审核的图片。" : filter === "archived" ? "没有已归档的图片。" : query ? "没有匹配的素材，换个关键词试试。" : "素材库还是空的。"}
            {filter !== "archived" && !query && <button className="btn primary sm" onClick={() => setPanel("upload")}>上传图片</button>}
          </div>
        ) : (
          <div className="ml-grid">
            {visible.map((item) => {
              const state = assetState(item);
              return (
                <article key={item.id} className={`ml-card ${state}`}>
                  <a className="ml-thumb" href={item.file_url} target="_blank" rel="noreferrer" title="查看原图">
                    <img src={item.file_url} alt={item.alt_text || item.title} loading="lazy" />
                    <span className={`ml-badge ${state}`}>
                      {state === "pending" ? "待审核" : state === "approved" ? "可用" : state === "rejected" ? "已驳回" : "已归档"}
                    </span>
                    {item.source_type === "generated" && <span className="ml-badge ai">AI</span>}
                  </a>
                  <div className="ml-card-body">
                    <div className="ml-card-title" title={item.title}>{item.title}</div>
                    <div className="ml-meta">
                      {item.width}×{item.height} · {formatBytes(item.byte_size)}
                      {item.quality_score != null && (
                        <span className="ml-score" title={(item.quality_details?.reasons ?? []).join("；") || "清晰度、构图、亮度等的自动初筛，不代表内容是否合适"}>
                          初筛 {item.quality_score}
                        </span>
                      )}
                    </div>
                    {item.tags.length > 0 && (
                      <div className="ml-tags">
                        {item.tags.slice(0, 3).map((tag) => <span className="tag gray" key={tag}>{tagLabel(tag)}</span>)}
                        {item.tags.length > 3 && <span className="tag gray">+{item.tags.length - 3}</span>}
                      </div>
                    )}
                    <div className="ml-card-actions">
                      {state === "pending" && (
                        <>
                          <button className="btn sm success" onClick={() => void review(item, "approved")} disabled={isPending("media:review:" + item.id)}>通过</button>
                          <button className="btn sm" onClick={() => void review(item, "rejected")} disabled={isPending("media:review:" + item.id)}>驳回</button>
                        </>
                      )}
                      {state === "approved" && (
                        <>
                          <button className="btn sm primary" onClick={() => void assignToContent(item)}
                            disabled={!selectedContentId || isPending("media:use:" + item.id)}
                            title={selectedContentId ? `设为所选内容的${ROLE_LABEL[usageRole]}` : "先在上方选择要配图的内容"}>配给这篇</button>
                          <button className="btn sm" onClick={() => startEdit(item)} disabled={!aiReady} title={aiReady ? "基于这张图改图" : aiBlockedReason}>改图</button>
                        </>
                      )}
                      {state !== "archived" && (
                        <button className="btn sm ghost ml-more" onClick={() => void archive(item)} disabled={isPending(`media:archive:${item.id}`)}>归档</button>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
