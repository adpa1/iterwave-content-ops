import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../AppContext";
import { api, MediaAsset, MediaUsageTarget, MediaGenerationSettings, MediaRecommendation } from "../api";
import { platformMeta } from "../platforms";

type UsageRole = "cover" | "illustration" | "background";
type Panel = "none" | "upload" | "generate" | "settings";
type Filter = "all" | "pending" | "approved" | "rejected" | "archived";

const MAX_BYTES = 12 * 1024 * 1024;
// 通用用途分类（不绑定具体行业），与推荐接口的用途匹配词保持一致。
const PURPOSES = ["封面主视觉", "产品/作品", "场景环境", "人物/团队", "过程/细节", "活动宣传", "品牌元素", "信息图示", "背景氛围", "其他"];
const ROLE_LABEL: Record<UsageRole, string> = { cover: "封面", illustration: "正文配图", background: "背景" };
const DRAFT_STATUS_LABEL: Record<string, string> = { draft_pending: "待保存草稿", draft_saved: "平台草稿已存" };
const VISUAL_LABEL: Record<string, string> = { media_asset: "已用素材图", generated: "已用 AI 配图", degraded: "仅有模板图" };
const PLATFORM_CODES = new Set(["xhs", "douyin", "kuaishou", "wechat_channels"]);

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
  const [usageRole, setUsageRole] = useState<UsageRole>("illustration");
  const [settings, setSettings] = useState<MediaGenerationSettings | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [targetWarning, setTargetWarning] = useState("");

  // 上传
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [purpose, setPurpose] = useState("场景环境");
  const [title, setTitle] = useState("");
  const [altText, setAltText] = useState("");
  const [tagsText, setTagsText] = useState("");
  const fileInput = useRef<HTMLInputElement | null>(null);

  // AI 生成 / 改图
  const [prompt, setPrompt] = useState("");
  const [genTitle, setGenTitle] = useState("");
  const [editTarget, setEditTarget] = useState<MediaAsset | null>(null);

  // 推荐
  const [recommendations, setRecommendations] = useState<MediaRecommendation[]>([]);
  const [recommendationMessage, setRecommendationMessage] = useState("");
  const [recommendationKey, setRecommendationKey] = useState("");
  const [recommendationLoading, setRecommendationLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [result, targetResult, settingsResult] = await Promise.all([
        api.mediaAssets(query, "all"), api.mediaUsageTargets(), api.mediaGenerationSettings(),
      ]);
      setItems(result.items);
      setTargets(targetResult.items);
      setSettings(settingsResult);
      const requestedContentId = new URLSearchParams(window.location.search).get("focusContent");
      if (requestedContentId) {
        if (targetResult.items.some((item) => item.id === requestedContentId)) {
          setSelectedContentId(requestedContentId);
          setTargetWarning("");
        } else {
          setSelectedContentId("");
          setTargetWarning("从编辑页跳转的这篇内容当前不能配图（可能已发布或不在草稿状态）。请手动选择要配图的草稿。");
        }
      } else {
        setTargetWarning("");
        setSelectedContentId((current) => (
          current && targetResult.items.some((item) => item.id === current) ? current : targetResult.items[0]?.id ?? ""
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

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: 0, pending: 0, approved: 0, rejected: 0, archived: 0 };
    for (const item of items) {
      const state = assetState(item);
      c[state] += 1;
      if (state !== "archived") c.all += 1;
    }
    return c;
  }, [items]);

  const visible = useMemo(() => items.filter((item) => {
    const state = assetState(item);
    return filter === "all" ? state !== "archived" : state === filter;
  }), [items, filter]);

  const aiReady = Boolean(settings?.enabled && settings?.provider_configured);
  const aiBlockedReason = !settings ? "正在加载 AI 配图设置" : !settings.provider_configured
    ? "服务器尚未配置图片服务，请联系管理员" : !settings.enabled ? "AI 配图未启用，可在「AI 设置」中开启" : "";
  const quotaLeft = settings ? Math.max(0, settings.monthly_quota - settings.used_this_month) : 0;
  const selectedTarget = targets.find((t) => t.id === selectedContentId);

  const togglePanel = (next: Panel) => setPanel((current) => (current === next ? "none" : next));

  // ---------- 上传 ----------
  const chooseFiles = (next: File[]) => {
    const images = next.filter((f) => /^image\/(jpeg|png|webp|gif)$/.test(f.type));
    if (images.length < next.length) showToast(`已忽略 ${next.length - images.length} 个非图片文件（支持 JPG、PNG、WebP、GIF）`, "info");
    setFiles(images);
    setTitle(images.length === 1 ? images[0].name.replace(/\.[^.]+$/, "") : "");
  };

  const resetUpload = () => {
    setFiles([]); setTitle(""); setAltText(""); setTagsText("");
    if (fileInput.current) fileInput.current.value = "";
  };

  const upload = async () => {
    if (!files.length || isPending("media:upload")) return;
    const invalid = files.filter((item) => item.size <= 0 || item.size > MAX_BYTES);
    if (invalid.length) {
      showToast(`有 ${invalid.length} 张图片超过 12MB 或为空，请移除后再上传`, "error");
      return;
    }
    await withPending("media:upload", async () => {
      let succeeded = 0, duplicates = 0;
      const failedFiles: File[] = [];
      const customTags = splitTags(tagsText);
      for (const item of files) {
        try {
          const result = await api.uploadMediaAsset({
            filename: item.name,
            title: files.length === 1 && title.trim() ? title.trim() : item.name.replace(/\.[^.]+$/, ""),
            alt_text: altText.trim(),
            tags: Array.from(new Set([purpose, ...customTags])).slice(0, 30),
            data_base64: await fileAsBase64(item),
          });
          if (result.duplicate) duplicates += 1; else succeeded += 1;
        } catch (e) {
          failedFiles.push(item);
          console.error("media upload failed:", item.name, e);
        }
      }
      const parts = [`新增 ${succeeded} 张`];
      if (duplicates) parts.push(`${duplicates} 张已存在，已复用`);
      if (failedFiles.length) parts.push(`${failedFiles.length} 张失败，已保留可重试`);
      showToast(`上传完成：${parts.join("，")}`, failedFiles.length ? "info" : "success");
      if (failedFiles.length) setFiles(failedFiles);
      else { resetUpload(); setPanel("none"); }
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

        {panel === "upload" && (
          <div className="ml-drawer">
            <div
              className={`ml-drop ${dragging ? "over" : ""} ${files.length ? "has-files" : ""}`}
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); chooseFiles(Array.from(e.dataTransfer.files)); }}
              role="button" tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") fileInput.current?.click(); }}
            >
              <input ref={fileInput} type="file" multiple hidden accept="image/jpeg,image/png,image/webp,image/gif"
                onChange={(e) => chooseFiles(Array.from(e.target.files ?? []))} />
              {files.length === 0 ? (
                <>
                  <div className="ml-drop-title">拖入图片，或点击选择</div>
                  <div className="ml-hint">支持 JPG、PNG、WebP、GIF，可多选，单张不超过 12MB；重复图片会自动复用</div>
                </>
              ) : (
                <>
                  <div className="ml-drop-title">已选 {files.length} 张</div>
                  <div className="ml-hint">
                    {files.slice(0, 3).map((f) => `${f.name}（${formatBytes(f.size)}）`).join("、")}{files.length > 3 ? ` 等` : ""} · 点击重新选择
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
