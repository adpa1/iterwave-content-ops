/**
 * 可编辑配置面板（系统设置 / 首次向导共用）。
 *
 * 所有字段都走后端白名单 PATCH /api/config（写 config_changes 审计 + 立即生效），
 * API Key 单独走 POST /api/config/llm_key 写入 config/.env（R23 不回传明文）。
 * 这里的组件只提交「真正改动」的字段，未改动不提交。
 */
import { useState } from "react";
import { api, type SystemConfigView } from "../api";
import { useApp } from "../AppContext";

const PLATFORM_LABELS: Record<string, string> = {
  xhs: "小红书",
  douyin: "抖音",
  kuaishou: "快手",
  wechat_channels: "视频号",
};

export const KB_TYPES = ["auto", "sqlite", "csv", "jsonl"] as const;
export const KB_MAPPING_FIELDS: string[] = ["source", "title", "summary", "link", "date", "dimension"];

/** "a, b，c" → ["a","b","c"] */
function splitList(text: string): string[] {
  return text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
}

const inputStyle = {
  width: "100%",
  padding: "8px 12px",
  background: "var(--card)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  color: "var(--text)",
  fontSize: 13,
} as const;

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
}

function SaveBar({ onSave, busy, hint }: { onSave: () => void; busy: boolean; hint?: string }) {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14, flexWrap: "wrap" }}>
      <button className="btn primary sm" onClick={onSave} disabled={busy}>
        {busy ? "保存中…" : "保存"}
      </button>
      {hint && <span style={{ fontSize: 11, color: "var(--subtle)" }}>{hint}</span>}
    </div>
  );
}

// =====================================================================
// 账号画像：定位关键词 / 禁区方向
// =====================================================================
export function PersonaEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [keywords, setKeywords] = useState(view.readonly.persona_keywords.join(", "));
  const [forbidden, setForbidden] = useState(view.readonly.forbidden_directions.join(", "));
  const busy = isPending("cfg:persona");

  const save = async () => {
    const updates: Record<string, unknown> = {};
    const k = splitList(keywords);
    const f = splitList(forbidden);
    if (JSON.stringify(k) !== JSON.stringify(view.readonly.persona_keywords)) {
      updates["ultimate_goal.persona_keywords"] = k;
    }
    if (JSON.stringify(f) !== JSON.stringify(view.readonly.forbidden_directions)) {
      updates["ultimate_goal.forbidden_directions"] = f;
    }
    if (Object.keys(updates).length === 0) {
      showToast("画像没有改动", "info");
      return;
    }
    try {
      const res = await withPending("cfg:persona", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已保存 ${res.changed.length} 项画像配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field label="定位关键词（逗号分隔，最多 30 条）">
        <input style={inputStyle} value={keywords} onChange={(e) => setKeywords(e.target.value)}
          placeholder="如：AI 教育, 家长启蒙, 中小学, 工具效率" />
      </Field>
      <Field label="禁区方向（逗号分隔，最多 30 条）">
        <input style={inputStyle} value={forbidden} onChange={(e) => setForbidden(e.target.value)}
          placeholder="如：成人内容, 纯娱乐无教育价值" />
      </Field>
      <div style={{ fontSize: 12, color: "var(--muted)" }}>
        红线敏感词库 已内置 <span className="tag blue">{view.readonly.redline_keywords_count} 条</span>
        <span style={{ marginLeft: 8, color: "var(--subtle)" }}>（R22 硬锁，不可关闭）</span>
      </div>
      <SaveBar onSave={save} busy={busy} hint="保存后 R2 终极目标检查器立即按新画像拦截" />
    </div>
  );
}

// =====================================================================
// 选题库 KB：类型 / 路径 / 数据表
// =====================================================================
export function KbEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [kbType, setKbType] = useState(view.readonly.kb_type || "auto");
  const [path, setPath] = useState(view.readonly.kb_path || "");
  const [table, setTable] = useState(view.readonly.kb_table || "");
  const [mappingText, setMappingText] = useState(
    Object.entries(view.readonly.kb_field_mapping).map(([k, v]) => `${k}=${v}`).join(", "),
  );
  const [inspectMsg, setInspectMsg] = useState<string | null>(null);
  const busy = isPending("cfg:kb");

  /** "title=标题, dimension=分类" → {title:"标题", dimension:"分类"} */
  const parseMapping = (text: string): Record<string, string> | string => {
    const out: Record<string, string> = {};
    for (const part of text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean)) {
      const idx = part.indexOf("=");
      if (idx <= 0) return `字段映射格式错误：${part}（应为 通用字段=原始列名）`;
      const k = part.slice(0, idx).trim();
      const v = part.slice(idx + 1).trim();
      if (!KB_MAPPING_FIELDS.includes(k)) return `未知通用字段：${k}`;
      if (!v) return `${k} 的原始列名不能为空`;
      out[k] = v;
    }
    return out;
  };

  const save = async () => {
    if (!path.trim()) { showToast("选题库路径不能为空", "error"); return; }
    const mapping = parseMapping(mappingText);
    if (typeof mapping === "string") { showToast(mapping, "error"); return; }
    const updates: Record<string, unknown> = {};
    if (kbType !== view.readonly.kb_type) updates["topic_kb.type"] = kbType;
    if (path.trim() !== view.readonly.kb_path) updates["topic_kb.path"] = path.trim();
    if (table.trim() && table.trim() !== view.readonly.kb_table) updates["topic_kb.table_name"] = table.trim();
    if (JSON.stringify(mapping) !== JSON.stringify(view.readonly.kb_field_mapping)) {
      updates["topic_kb.field_mapping"] = mapping;
    }
    if (Object.keys(updates).length === 0) { showToast("选题库配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:kb", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 选题库配置已保存 ${res.changed.length} 项，下一轮选题立即生效`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  const inspect = async () => {
    setInspectMsg(null);
    try {
      const r = await api.kbInspect();
      setInspectMsg(r.ok
        ? `✅ ${r.stats?.total ?? 0} 条选题，${r.stats?.unique_dimensions ?? 0} 个维度（${r.file_type}${r.table ? ` · 表 ${r.table}` : ""}）`
        : `❌ ${r.error || "检查失败"}`);
    } catch (e: any) {
      setInspectMsg(`❌ 检查失败: ${e?.message ?? e}`);
    }
  };

  const mapping = Object.entries(view.readonly.kb_field_mapping);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field label="KB 类型">
        <select style={{ ...inputStyle, width: 200 }} value={kbType} onChange={(e) => setKbType(e.target.value)}>
          {KB_TYPES.map((t) => <option key={t} value={t}>{t === "auto" ? "auto（按扩展名自动识别）" : t}</option>)}
        </select>
      </Field>
      <Field label="文件路径（相对项目根目录或绝对路径）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={path}
          onChange={(e) => setPath(e.target.value)} placeholder="data/topics.db" />
      </Field>
      <Field label="数据表名（SQLite 专用）">
        <input style={{ ...inputStyle, width: 240, fontFamily: "monospace" }} value={table}
          onChange={(e) => setTable(e.target.value)} placeholder="topics" />
      </Field>
      <Field label="字段映射（通用字段=原始列名，逗号分隔；留空 = 自动识别）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={mappingText}
          onChange={(e) => setMappingText(e.target.value)}
          placeholder="title=标题, dimension=分类" />
      </Field>
      <div style={{ fontSize: 11, color: "var(--subtle)" }}>
        可用通用字段：{KB_MAPPING_FIELDS.join(" / ")}
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginTop: 4 }}>
        <SaveBar onSave={save} busy={busy} hint="路径改动会在下一次选题读取时生效，无需重启后端" />
        <button className="btn sm" onClick={inspect}>🔍 诊断选题库</button>
      </div>
      {inspectMsg && <div className="note">{inspectMsg}</div>}
    </div>
  );
}

// =====================================================================
// 主模型 / 兜底模型 / API Key
// =====================================================================
export function LlmEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [enabled, setEnabled] = useState(view.readonly.llm_enabled);
  const [provider, setProvider] = useState(view.readonly.llm_provider || "ark");
  const [model, setModel] = useState(view.readonly.llm_model || "");
  const [fallback, setFallback] = useState(view.readonly.llm_fallback_model || "");
  const [baseUrl, setBaseUrl] = useState(view.readonly.llm_base_url || "");
  const [temperature, setTemperature] = useState(String(view.readonly.llm_temperature ?? ""));
  const [timeoutSec, setTimeoutSec] = useState(String(view.readonly.llm_timeout_seconds ?? ""));
  const [apiKey, setApiKey] = useState("");
  const busy = isPending("cfg:llm");
  const keyBusy = isPending("cfg:llm_key");

  const saveModel = async () => {
    if (!model.trim()) { showToast("主模型不能为空", "error"); return; }
    const updates: Record<string, unknown> = {};
    if (enabled !== view.readonly.llm_enabled) updates["llm.enabled"] = enabled;
    if (provider.trim() !== view.readonly.llm_provider) updates["llm.provider"] = provider.trim();
    if (model.trim() !== view.readonly.llm_model) updates["llm.model"] = model.trim();
    if (fallback.trim() !== (view.readonly.llm_fallback_model || "")) {
      updates["llm.fallback_model"] = fallback.trim();
    }
    if (baseUrl.trim() && baseUrl.trim().replace(/\/+$/, "") !== (view.readonly.llm_base_url || "").replace(/\/+$/, "")) {
      updates["llm.base_url"] = baseUrl.trim();
    }
    const temp = Number(temperature);
    if (temperature !== "" && temp !== Number(view.readonly.llm_temperature)) {
      if (!Number.isFinite(temp) || temp < 0 || temp > 2) { showToast("temperature 必须在 0-2 之间", "error"); return; }
      updates["llm.temperature"] = temp;
    }
    const tsec = Number(timeoutSec);
    if (timeoutSec !== "" && tsec !== Number(view.readonly.llm_timeout_seconds)) {
      if (!Number.isInteger(tsec) || tsec < 5 || tsec > 300) { showToast("超时必须在 5-300 秒之间", "error"); return; }
      updates["llm.timeout_seconds"] = tsec;
    }
    if (Object.keys(updates).length === 0) { showToast("模型配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:llm", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已保存 ${res.changed.length} 项模型配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  const saveKey = async (value: string) => {
    try {
      const r = await withPending("cfg:llm_key", () => api.saveLlmKey(value));
      if (!r) return;
      setApiKey("");
      showToast(value ? "✅ API Key 已写入 config/.env（chmod 600）" : "✅ 已清除 API Key", "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存 Key 失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <label style={{ fontSize: 13, display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        启用 LLM（关闭时选题评分 / 文案生成自动降级为模板）
      </label>
      <Field label="Provider">
        <input style={{ ...inputStyle, width: 200 }} value={provider}
          onChange={(e) => setProvider(e.target.value)} placeholder="ark" />
      </Field>
      <Field label="主模型">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={model}
          onChange={(e) => setModel(e.target.value)} placeholder="doubao-seed-1-6-250615" />
      </Field>
      <Field label="兜底模型（可留空）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={fallback}
          onChange={(e) => setFallback(e.target.value)} placeholder="留空 = 不启用兜底" />
      </Field>
      <Field label="Base URL（按量付费 /api/v3 ↔ 专属通道 /api/plan/v3）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://ark.cn-beijing.volces.com/api/v3" />
      </Field>
      <div className="grid three" style={{ gap: 10 }}>
        <Field label="temperature（0-2）">
          <input className="field" type="number" step="0.1" min={0} max={2} style={{ width: "100%" }}
            value={temperature} onChange={(e) => setTemperature(e.target.value)} />
        </Field>
        <Field label="单次超时（秒，5-300）">
          <input className="field" type="number" min={5} max={300} style={{ width: "100%" }}
            value={timeoutSec} onChange={(e) => setTimeoutSec(e.target.value)} />
        </Field>
      </div>
      <SaveBar onSave={saveModel} busy={busy} />
      <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
        <Field label={`API Key（写 config/.env，前端不回显；当前：${view.readonly.llm_has_api_key ? "已配置" : "未配置"}）`}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <input style={{ ...inputStyle, flex: "1 1 240px", fontFamily: "monospace" }} type="password"
              value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="粘贴 Key 后点「保存 Key」" />
            <button className="btn primary sm" disabled={!apiKey.trim() || keyBusy}
              onClick={() => saveKey(apiKey)}>保存 Key</button>
            {view.readonly.llm_has_api_key && (
              <button className="btn sm" disabled={keyBusy} onClick={() => saveKey("")}>清除 Key</button>
            )}
          </div>
        </Field>
        <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 8 }}>
          R23：Key 只落 config/.env（chmod 600），不进日志、不进数据库、不返回前端。
        </div>
      </div>
    </div>
  );
}

// =====================================================================
// 平台启用开关
// =====================================================================
export function PlatformToggles({ view, onSaved, loggedIn }: {
  view: SystemConfigView;
  onSaved?: () => void;
  /** /api/status.logged_in_platforms，用于提示「已启用但未登录」 */
  loggedIn?: string[];
}) {
  const { showToast, withPending, isPending } = useApp();
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(view.readonly.platforms)) init[k] = !!v.enabled;
    return init;
  });
  const [baseUrls, setBaseUrls] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const [k, v] of Object.entries(view.readonly.platforms)) init[k] = v.base_url || "";
    return init;
  });
  const [accountKeys, setAccountKeys] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const [k, v] of Object.entries(view.readonly.platforms)) init[k] = v.account_key || "";
    return init;
  });
  const busy = isPending("cfg:platforms");

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(view.readonly.platforms)) {
      if (enabled[k] !== v.enabled) updates[`platforms.${k}.enabled`] = enabled[k];
      const url = (baseUrls[k] ?? "").trim();
      if (url && url !== v.base_url) updates[`platforms.${k}.base_url`] = url;
      const key = (accountKeys[k] ?? "").trim();
      if (key && key !== v.account_key) updates[`platforms.${k}.account_key`] = key;
    }
    if (Object.keys(updates).length === 0) { showToast("平台配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:platforms", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已更新 ${res.changed.length} 项平台配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>平台</th><th style={{ width: 70 }}>启用</th><th>account_key（登录态文件）</th><th>base_url（可改）</th><th>登录态</th></tr></thead>
          <tbody>
            {Object.entries(view.readonly.platforms).map(([k, v]) => (
              <tr key={k}>
                <td>{PLATFORM_LABELS[k] ?? k}</td>
                <td>
                  <input type="checkbox" checked={enabled[k] ?? false}
                    onChange={(e) => setEnabled((prev) => ({ ...prev, [k]: e.target.checked }))} />
                </td>
                <td>
                  <input className="field" style={{ width: "100%", fontFamily: "monospace", fontSize: 11 }}
                    value={accountKeys[k] ?? ""}
                    placeholder="xhs_main"
                    onChange={(e) => setAccountKeys((prev) => ({ ...prev, [k]: e.target.value }))} />
                </td>
                <td>
                  <input className="field" style={{ width: "100%", fontFamily: "monospace", fontSize: 11 }}
                    value={baseUrls[k] ?? ""}
                    onChange={(e) => setBaseUrls((prev) => ({ ...prev, [k]: e.target.value }))} />
                </td>
                <td>
                  {loggedIn?.includes(k)
                    ? <span className="tag green">已登录</span>
                    : <span className="tag orange">未登录</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <SaveBar onSave={save} busy={busy}
        hint="至少保留 1 个平台；启用 ≠ 可发布，还需在「账号连接」扫码登录" />
    </>
  );
}

// =====================================================================
// 高级参数：评分权重 / 半衰期 / 节奏间隔 / 红线词库
// 页面可调，但两条安全底线仍硬锁：R22 的「维度匹配」「可发布性」门禁开关
// =====================================================================
const SCORING_MODES: { key: "cold_start" | "sample_accumulating" | "stable_operation"; label: string; hint: string }[] = [
  { key: "cold_start", label: "冷启动", hint: "全库已发布样本 < 10" },
  { key: "sample_accumulating", label: "样本积累", hint: "全库 ≥10 且有维度样本 < 5" },
  { key: "stable_operation", label: "稳定运行", hint: "所有活跃维度 ≥5（含 15% ε-greedy）" },
];

const WEIGHT_FIELDS: [string, string][] = [
  ["position_match", "定位"],
  ["history_performance", "历史"],
  ["timeliness", "时效"],
  ["gap_weight", "缺口"],
  ["freshness", "新鲜度"],
];

const INTERVAL_FIELDS: [string, string][] = [
  ["passive_reply_base_seconds", "被动回复基准"],
  ["passive_reply_jitter_seconds", "被动回复抖动"],
  ["active_comment_base_seconds", "主动评论基准"],
  ["active_comment_jitter_seconds", "主动评论抖动"],
  ["follow_base_seconds", "关注基准"],
  ["follow_jitter_seconds", "关注抖动"],
];

export function AdvancedEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [weights, setWeights] = useState<Record<string, Record<string, string>>>(() => {
    const init: Record<string, Record<string, string>> = {};
    for (const m of SCORING_MODES) {
      init[m.key] = {};
      for (const [f] of WEIGHT_FIELDS) init[m.key][f] = String(view.readonly.scoring[m.key][f] ?? 0);
    }
    return init;
  });
  const [halfLife, setHalfLife] = useState<Record<string, string>>({
    news: String(view.readonly.scoring.timeliness_half_life_days.news ?? ""),
    evergreen: String(view.readonly.scoring.timeliness_half_life_days.evergreen ?? ""),
  });
  const [intervals, setIntervals] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const [f] of INTERVAL_FIELDS) init[f] = String(view.readonly.rhythm_intervals[f] ?? 0);
    return init;
  });
  const [meta, setMeta] = useState<Record<string, string>>({
    active_dimension_min_uses: String(view.readonly.scoring.active_dimension_min_uses ?? ""),
    active_dimension_window_days: String(view.readonly.scoring.active_dimension_window_days ?? ""),
    shrinkage_m: String(view.readonly.scoring.shrinkage_m ?? ""),
  });
  const [redline, setRedline] = useState(view.readonly.redline_keywords.join(", "));
  const busy = isPending("cfg:advanced");

  const modeSum = (mode: string) =>
    WEIGHT_FIELDS.reduce((acc, [f]) => acc + (Number(weights[mode]?.[f]) || 0), 0);

  const save = async () => {
    const updates: Record<string, unknown> = {};

    for (const m of SCORING_MODES) {
      const total = modeSum(m.key);
      if (total !== 100) {
        showToast(`${m.label} 权重合计 ${total}%，必须 = 100%`, "error");
        return;
      }
      for (const [f] of WEIGHT_FIELDS) {
        const n = Number(weights[m.key][f]);
        if (!Number.isInteger(n) || n < 0 || n > 100) {
          showToast(`${m.label} · ${f} 必须是 0-100 整数`, "error");
          return;
        }
        if (n !== view.readonly.scoring[m.key][f]) {
          updates[`scoring.${m.key}.${f}`] = n;
        }
      }
    }

    for (const kind of ["news", "evergreen"] as const) {
      const n = Number(halfLife[kind]);
      if (!Number.isInteger(n) || n < 1) { showToast("半衰期必须是 ≥1 的整数（天）", "error"); return; }
      if (n !== view.readonly.scoring.timeliness_half_life_days[kind]) {
        updates[`scoring.timeliness_half_life_days.${kind}`] = n;
      }
    }

    for (const [f, label] of [
      ["active_dimension_min_uses", "活跃维度最少出现次数"],
      ["active_dimension_window_days", "活跃维度窗口（天）"],
      ["shrinkage_m", "收缩估计 m"],
    ] as [string, string][]) {
      const n = Number(meta[f]);
      if (!Number.isInteger(n) || n < 1) { showToast(`${label} 必须是 ≥1 的整数`, "error"); return; }
      if (n !== Number((view.readonly.scoring as unknown as Record<string, number>)[f])) {
        updates[`scoring.${f}`] = n;
      }
    }

    for (const [f] of INTERVAL_FIELDS) {
      const n = Number(intervals[f]);
      if (!Number.isInteger(n) || n < 0 || n > 3600) {
        showToast(`${f} 必须是 0-3600 的整数（秒）`, "error"); return;
      }
      if (n !== view.readonly.rhythm_intervals[f]) updates[`rhythm.intervals.${f}`] = n;
    }

    const redlineList = splitList(redline);
    if (Math.max(0, ...redlineList.map((s) => s.length)) > 20) {
      showToast("红线词单条最长 20 字", "error"); return;
    }
    if (JSON.stringify(redlineList) !== JSON.stringify(view.readonly.redline_keywords)) {
      updates["ultimate_goal.redline_keywords"] = redlineList;
    }

    if (Object.keys(updates).length === 0) { showToast("高级参数没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:advanced", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已保存 ${res.changed.length} 项高级参数`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
          三模式评分权重（R11 · 每个模式合计必须 = 100%）
        </div>
        <div className="grid three" style={{ gap: 10 }}>
          {SCORING_MODES.map((m) => {
            const total = modeSum(m.key);
            return (
              <div key={m.key} style={{ padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                  <strong style={{ fontSize: 12 }}>{m.label}</strong>
                  <span className={`tag ${total === 100 ? "green" : "red"}`}>{total}%</span>
                </div>
                <div className="metric-foot" style={{ marginBottom: 8 }}>{m.hint}</div>
                {WEIGHT_FIELDS.map(([f, label]) => (
                  <div key={f} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontSize: 11, color: "var(--muted)", width: 42 }}>{label}</span>
                    <input className="field" type="number" min={0} max={100} style={{ width: 70 }}
                      value={weights[m.key]?.[f] ?? ""}
                      onChange={(e) => setWeights((prev) => ({
                        ...prev, [m.key]: { ...prev[m.key], [f]: e.target.value },
                      }))} />
                    <span style={{ fontSize: 11, color: "var(--subtle)" }}>%</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <Field label="时效半衰期 · 新闻（天）">
          <input className="field" type="number" min={1} style={{ width: 100 }} value={halfLife.news}
            onChange={(e) => setHalfLife((p) => ({ ...p, news: e.target.value }))} />
        </Field>
        <Field label="时效半衰期 · 常青（天）">
          <input className="field" type="number" min={1} style={{ width: 100 }} value={halfLife.evergreen}
            onChange={(e) => setHalfLife((p) => ({ ...p, evergreen: e.target.value }))} />
        </Field>
        <Field label="活跃维度：最少出现次数">
          <input className="field" type="number" min={1} max={10} style={{ width: 100 }}
            value={meta.active_dimension_min_uses}
            onChange={(e) => setMeta((p) => ({ ...p, active_dimension_min_uses: e.target.value }))} />
        </Field>
        <Field label="活跃维度窗口（天）">
          <input className="field" type="number" min={7} max={365} style={{ width: 100 }}
            value={meta.active_dimension_window_days}
            onChange={(e) => setMeta((p) => ({ ...p, active_dimension_window_days: e.target.value }))} />
        </Field>
        <Field label="收缩估计 m">
          <input className="field" type="number" min={1} max={50} style={{ width: 100 }}
            value={meta.shrinkage_m}
            onChange={(e) => setMeta((p) => ({ ...p, shrinkage_m: e.target.value }))} />
        </Field>
      </div>

      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
          节奏随机间隔（秒：实际间隔 = 基准 ± 抖动随机）
        </div>
        <div className="grid three" style={{ gap: 10 }}>
          {INTERVAL_FIELDS.map(([f, label]) => (
            <Field key={f} label={label}>
              <input className="field" type="number" min={0} max={3600} style={{ width: "100%" }}
                value={intervals[f] ?? ""}
                onChange={(e) => setIntervals((prev) => ({ ...prev, [f]: e.target.value }))} />
            </Field>
          ))}
        </div>
      </div>

      <Field label="红线词库（逗号分隔，最多 200 条；出现即丢弃重生成）">
        <textarea style={{ ...inputStyle, minHeight: 64, fontFamily: "monospace" }} value={redline}
          onChange={(e) => setRedline(e.target.value)} placeholder="如：最, 第一, 绝对, 100%" />
      </Field>

      <div className="note" style={{ fontSize: 11 }}>
        🔒 R22 两条门禁开关（维度匹配 / 可发布性）在代码层硬锁，任何配置都无法关闭；这里改的是词库与阈值，不是开关。
      </div>

      <SaveBar onSave={save} busy={busy} hint="保存后立即生效并写入 config_changes 审计" />
    </div>
  );
}

// =====================================================================
// 阶段超时（R8：每个参数只管自己的阶段）
// =====================================================================
const TIMEOUT_FIELDS: { key: string; label: string; scope: string }[] = [
  { key: "round_timeout_hours", label: "producing 超时", scope: "仅 producing" },
  { key: "awaiting_publish_timeout_hours", label: "待发布超时", scope: "仅 awaiting_publish" },
  { key: "publish_confirm_timeout_hours", label: "单篇确认超时", scope: "per-draft" },
  { key: "publish_confirm_grace_hours", label: "确认宽限期", scope: "per-draft" },
  { key: "collecting_metrics_timeout_hours", label: "采集超时", scope: "仅 collecting_metrics" },
  { key: "metrics_initial_delay_hours", label: "发布后等数据", scope: "user_published 后" },
];

export function TimeoutEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of TIMEOUT_FIELDS) init[f.key] = String(view.readonly.timeouts[f.key] ?? "");
    return init;
  });
  const busy = isPending("cfg:timeouts");

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const f of TIMEOUT_FIELDS) {
      const raw = values[f.key];
      if (raw === "") continue;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 0) { showToast(`${f.label} 必须是非负整数`, "error"); return; }
      if (n !== Number(view.readonly.timeouts[f.key])) updates[`timeouts.${f.key}`] = n;
    }
    if (Object.keys(updates).length === 0) { showToast("超时配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:timeouts", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已保存 ${res.changed.length} 项超时配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <>
      <div className="grid three">
        {TIMEOUT_FIELDS.map((f) => (
          <div className="metric" key={f.key}>
            <div className="metric-label">{f.label}</div>
            <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 6 }}>
              <input className="field" type="number" min={0} style={{ width: 90 }}
                value={values[f.key]}
                onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))} />
              <span className="metric-foot">小时</span>
            </div>
            <div className="metric-foot">{f.scope}</div>
          </div>
        ))}
      </div>
      <SaveBar onSave={save} busy={busy}
        hint="R8：producing 超时必须小于待发布超时；各阶段独立计时，不互相覆盖" />
    </>
  );
}

// =====================================================================
// 通知通道开关（企微/飞书 webhook URL 只从 .env 读，R23 不回传明文）
// =====================================================================
const NOTIFY_CHANNELS: [string, string][] = [
  ["macos", "macOS 原生通知"],
  ["wecom", "企业微信 webhook"],
  ["feishu", "飞书 webhook"],
];

export function NotifyChannelsEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const [k] of NOTIFY_CHANNELS) init[k] = !!view.readonly.notification_channels[k]?.enabled;
    return init;
  });
  const busy = isPending("cfg:notify_channels");

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const [k] of NOTIFY_CHANNELS) {
      const cur = !!view.readonly.notification_channels[k]?.enabled;
      if (enabled[k] !== cur) updates[`notifications.channels.${k}.enabled`] = enabled[k];
    }
    if (Object.keys(updates).length === 0) { showToast("通知通道没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:notify_channels", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✅ 已更新 ${res.changed.length} 个通知通道`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>通道</th><th style={{ width: 70 }}>启用</th><th>.env 配置</th></tr></thead>
          <tbody>
            {NOTIFY_CHANNELS.map(([k, label]) => {
              const ch = view.readonly.notification_channels[k];
              return (
                <tr key={k}>
                  <td>{label}</td>
                  <td>
                    <input type="checkbox" checked={enabled[k] ?? false}
                      onChange={(e) => setEnabled((prev) => ({ ...prev, [k]: e.target.checked }))} />
                  </td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                    {k === "macos"
                      ? <span className="tag gray">无需配置</span>
                      : ch?.has_webhook_url
                        ? <span className="tag green">{ch.webhook_url_env} 已设置</span>
                        : <span className="tag orange">{ch?.webhook_url_env || "未配置"} 缺失（.env）</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <SaveBar onSave={save} busy={busy}
        hint="启用只是允许发送；Webhook URL 仍只存 config/.env（R23，绝不回传页面）" />
    </>
  );
}
