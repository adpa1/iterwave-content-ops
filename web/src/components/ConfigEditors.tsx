/**
 * 可编辑配置面板（系统设置 / 首次向导共用）。
 *
 * 所有字段都走后端白名单 PATCH /api/config（SQLite 配置版本 + config_changes 审计 + 立即生效），
 * 配置字段元数据可通过 GET /api/config/metadata 查询；服务密钥通过独立接口保存到服务端，前端不会读取或回显明文。
 * 这里的组件只提交「真正改动」的字段，未改动不提交。
 */
import { useEffect, useState } from "react";
import { api, type Platform, type PlatformStyle, type SystemConfigView } from "../api";
import { useApp } from "../AppContext";
import { navigate } from "../nav";

const PLATFORM_LABELS: Record<string, string> = {
  xhs: "小红书",
  douyin: "抖音",
  kuaishou: "快手",
  wechat_channels: "视频号",
};

export const KB_TYPES = ["auto", "sqlite", "csv", "json", "jsonl", "xlsx", "rss"] as const;
const KB_TYPE_LABELS: Record<string, string> = {
  auto: "自动识别", sqlite: "数据库", csv: "CSV 表格", json: "JSON 文件",
  jsonl: "JSONL 文件", xlsx: "Excel 表格", rss: "RSS 订阅",
};
export const KB_MAPPING_FIELDS: string[] = ["source", "title", "summary", "link", "date", "dimension"];
const KB_FIELD_LABELS: Record<string, string> = {
  source: "来源", title: "标题", summary: "摘要", link: "原文链接",
  date: "日期", dimension: "内容方向",
};
const KB_FIELD_KEYS = Object.fromEntries(
  Object.entries(KB_FIELD_LABELS).map(([key, label]) => [label, key]),
) as Record<string, string>;

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
      showToast(`✓ 已保存 ${res.changed.length} 项画像配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field label="定位关键词（逗号分隔，最多 30 条）">
        <input style={inputStyle} value={keywords} onChange={(e) => setKeywords(e.target.value)}
          placeholder="如：职场成长, 效率方法, 生活方式" />
      </Field>
      <Field label="禁区方向（逗号分隔，最多 30 条）">
        <input style={inputStyle} value={forbidden} onChange={(e) => setForbidden(e.target.value)}
          placeholder="如：纯娱乐无信息增量, 医疗诊断结论" />
      </Field>
      <div style={{ fontSize: 12, color: "var(--muted)" }}>
        红线敏感词库 已内置 <span className="tag blue">{view.readonly.redline_keywords_count} 条</span>
        <span style={{ marginLeft: 8, color: "var(--subtle)" }}></span>
      </div>
      <SaveBar onSave={save} busy={busy} hint="保存后 终极目标检查器立即按新画像拦截" />
    </div>
  );
}

// =====================================================================
// 选题库 KB：类型 / 路径 / 数据表
// 路径来自后端脱敏（项目外绝对路径显示为 ~/…/x 或 …/x），脱敏值不能回写，
// 所以脱敏状态下只读，统一到「内容资产 → 知识库来源」里维护。
//
// =====================================================================
export function KbEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [kbType, setKbType] = useState(view.readonly.kb_type || "auto");
  const [path, setPath] = useState(view.readonly.kb_path || "");
  const [table, setTable] = useState(view.readonly.kb_table || "");
  const [mappingText, setMappingText] = useState(
    Object.entries(view.readonly.kb_field_mapping).map(([k, v]) => (KB_FIELD_LABELS[k] ?? k) + "=" + v).join(", "),
  );
  const [inspectMsg, setInspectMsg] = useState<string | null>(null);
  const busy = isPending("cfg:kb");
  /** 脱敏后的路径（含 …）不可回写，避免把展示值当成真实路径保存 */
  const pathRedacted = true;

  /** "title=标题, dimension=分类" → {title:"标题", dimension:"分类"} */
  const parseMapping = (text: string): Record<string, string> | string => {
    const out: Record<string, string> = {};
    for (const part of text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean)) {
      const idx = part.indexOf("=");
      if (idx <= 0) return `字段映射格式错误：${part}（应为 通用字段=原始列名）`;
      const rawKey = part.slice(0, idx).trim();
      const k = KB_FIELD_KEYS[rawKey] ?? rawKey;
      const v = part.slice(idx + 1).trim();
      if (!KB_MAPPING_FIELDS.includes(k)) return `未知字段名称：${rawKey}（可用：${Object.values(KB_FIELD_LABELS).join("、")}）`;
      if (!v) return `${k} 的原始列名不能为空`;
      out[k] = v;
    }
    return out;
  };

  const save = async () => {
    const mapping = parseMapping(mappingText);
    if (typeof mapping === "string") { showToast(mapping, "error"); return; }
    const updates: Record<string, unknown> = {};
    if (kbType !== view.readonly.kb_type) updates["topic_kb.type"] = kbType;
    if (table.trim() && table.trim() !== view.readonly.kb_table) updates["topic_kb.table_name"] = table.trim();
    if (JSON.stringify(mapping) !== JSON.stringify(view.readonly.kb_field_mapping)) {
      updates["topic_kb.field_mapping"] = mapping;
    }
    if (Object.keys(updates).length === 0) { showToast("选题库配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:kb", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✓ 选题库配置已保存 ${res.changed.length} 项，下一轮选题立即生效`, "success");
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
        ? `✓ ${r.stats?.total ?? 0} 条选题，${r.stats?.unique_dimensions ?? 0} 个维度（${r.file_type}${r.table ? ` · 表 ${r.table}` : ""}）`
        : `✕ ${r.error || "检查失败"}`);
    } catch (e: any) {
      setInspectMsg(`✕ 检查失败: ${e?.message ?? e}`);
    }
  };

  const mapping = Object.entries(view.readonly.kb_field_mapping);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field label="选题库数据格式">
        <select style={{ ...inputStyle, width: 200 }} value={kbType} onChange={(e) => setKbType(e.target.value)}>
          {KB_TYPES.map((t) => <option key={t} value={t}>{KB_TYPE_LABELS[t]}</option>)}
        </select>
      </Field>
      <Field label="选题库文件位置（只读）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={path}
          readOnly={pathRedacted}
          onChange={(e) => setPath(e.target.value)} placeholder="由内容资产中的选题库来源管理" />
      </Field>
      <div className="note" style={{ fontSize: 11 }}>
        选题库可以来自多个来源（本地上传 / 远程地址 / 服务器路径），统一在
        <button type="button" className="btn sm" style={{ marginLeft: 6 }}
          onClick={() => navigate("assets", "mapping")}>内容资产 → 知识库来源</button>
        管理，支持自动识别字段与预览。
      </div>
      {kbType === "sqlite" && <Field label="数据表名称">
        <input style={{ ...inputStyle, width: 240, fontFamily: "monospace" }} value={table}
          onChange={(e) => setTable(e.target.value)} placeholder="输入要读取的数据表名称" />
      </Field>}
      <Field label="列名对应关系（中文字段=来源列名，逗号分隔；留空 = 自动识别）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={mappingText}
          onChange={(e) => setMappingText(e.target.value)}
          placeholder="标题=内容标题, 内容方向=分类" />
      </Field>
      <div style={{ fontSize: 11, color: "var(--subtle)" }}>
        可用字段：{Object.values(KB_FIELD_LABELS).join(" / ")}
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginTop: 4 }}>
        <SaveBar onSave={save} busy={busy} hint="保存后立即更新配置；下一轮运行读取选题时使用新路径，无需重启后端" />
        <button className="btn sm" onClick={inspect}>⌕ 诊断选题库</button>
      </div>
      {inspectMsg && <div className="note">{inspectMsg}</div>}
    </div>
  );
}

// =====================================================================
// 主模型 / 兜底模型 / API Key
// =====================================================================
// 火山方舟两条计费通道：按量付费 /、Agent Plan 专属通道。
// Agent Plan 只支持套餐内模型（见 https://www.volcengine.com/docs/82379/2366394），
// 用不支持的模型会返回 UnsupportedModel 错误。
const ARK_CHANNEL_BASE: Record<string, string> = {
  metered: "https://ark.cn-beijing.volces.com/api/v3",
  plan: "https://ark.cn-beijing.volces.com/api/plan/v3",
};
const ARK_PLAN_MODELS = [
  "doubao-seed-2.1-lite",
  "doubao-seed-2.0-mini",
  "doubao-seed-2.1-pro",
  "deepseek-v4.1-flash",
  "deepseek-v4.1-flash-economy",
  "glm-5.3-flash",
  "kimi-k3",
  "kimi-k3.1-pro",
];

export function LlmEditor({ view, onSaved }: {
  view: SystemConfigView; onSaved?: () => void;
}) {
  const { showToast, withPending, isPending } = useApp();
  const [enabled, setEnabled] = useState(view.readonly.llm_enabled);
  const [provider, setProvider] = useState(view.readonly.llm_provider || "ark");
  const [model, setModel] = useState(view.readonly.llm_model || "");
  const [fallback, setFallback] = useState(view.readonly.llm_fallback_model || "");
  const [channel, setChannel] = useState<"plan" | "metered" | "custom">(
    view.readonly.llm_channel || "metered",
  );
  const [baseUrl, setBaseUrl] = useState("");
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
    if (baseUrl.trim()) updates["llm.base_url"] = baseUrl.trim();
    const temp = Number(temperature);
    if (temperature !== "" && temp !== Number(view.readonly.llm_temperature)) {
      if (!Number.isFinite(temp) || temp < 0 || temp > 2) { showToast("生成随机度必须在 0-2 之间", "error"); return; }
      updates["llm.temperature"] = temp;
    }
    const tsec = Number(timeoutSec);
    if (timeoutSec !== "" && tsec !== Number(view.readonly.llm_timeout_seconds)) {
      if (!Number.isInteger(tsec) || tsec < 5 || tsec > 300) { showToast("超时必须在 5-300 秒之间", "error"); return; }
      updates["llm.timeout_seconds"] = tsec;
    }
    if (Object.keys(updates).length === 0) { showToast("模型配置没有改动", "info"); return; }
    // llm.* 属于系统级生产配置；快照校验会停止所有账号组中尚未执行的旧配置任务。
    // 先读取全局队列并明确告知影响范围，避免用户误以为只影响当前账号组。
    if (Object.keys(updates).some((key) => key.startsWith("llm."))) {
      try {
        const queue = await api.taskQueue();
        const queued = queue.items.filter((item) => item.status === "queued");
        if (queued.length > 0) {
          const byTenant = new Map<string, { name: string; count: number }>();
          for (const task of queued) {
            const current = byTenant.get(task.tenant_id) ?? {
              name: task.tenant_name || task.tenant_id, count: 0,
            };
            current.count += 1;
            byTenant.set(task.tenant_id, current);
          }
          const scope = [...byTenant.values()]
            .map((item) => `「${item.name}」${item.count} 个`)
            .join("、");
          const confirmed = window.confirm(
            `本次修改是系统级模型配置，会使所有账号组中尚未开始执行的旧配置任务因快照不匹配而停止。\n\n待执行任务：${queued.length} 个（${scope}）。\n\n建议先等待队列清空。仍要保存模型配置吗？`,
          );
          if (!confirmed) return;
        }
      } catch (error) {
        showToast("无法读取全局任务队列，已取消模型配置保存；请刷新队列后重试。", "error");
        return;
      }
    }
    try {
      const res = await withPending("cfg:llm", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✓ 已保存 ${res.changed.length} 项模型配置`, "success");
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
      showToast(value ? "✓ 服务密钥已安全保存" : "✓ 已清除 API Key", "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存密钥失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <label style={{ fontSize: 13, display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        启用大模型（关闭时选题评分 / 文案生成自动降级为模板）
      </label>
      <Field label="模型服务商">
        <input style={{ ...inputStyle, width: 200 }} value={provider}
          onChange={(e) => setProvider(e.target.value)} placeholder="ark" />
      </Field>
      <Field label="计费通道（火山方舟）">
        <select className="field" style={{ ...inputStyle }} value={channel}
          onChange={(e) => {
            const next = e.target.value as "plan" | "metered" | "custom";
            setChannel(next);
            if (ARK_CHANNEL_BASE[next]) setBaseUrl(ARK_CHANNEL_BASE[next]);
          }}>
          <option value="plan">专属 Agent Plan</option>
          <option value="metered">按量付费</option>
          <option value="custom">自定义端点</option>
        </select>
      </Field>
      <Field label="主模型（Agent Plan 套餐只支持套餐内模型，选错会报模型不支持）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={model}
          list="ark-plan-models" onChange={(e) => setModel(e.target.value)}
          placeholder="doubao-seed-2.1-lite" />
        <datalist id="ark-plan-models">
          {ARK_PLAN_MODELS.map((m) => <option key={m} value={m} />)}
        </datalist>
      </Field>
      <Field label="兜底模型（可留空）">
        <input style={{ ...inputStyle, fontFamily: "monospace" }} value={fallback}
          onChange={(e) => setFallback(e.target.value)} placeholder="留空 = 不启用兜底" />
      </Field>
      {channel === "custom" ? (
        <Field label="Base URL（自定义端点）">
          <input style={{ ...inputStyle, fontFamily: "monospace" }} value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="已配置的服务地址不会回显；输入新地址以替换" />
        </Field>
      ) : (
        <div style={{ fontSize: 12, color: "var(--subtle)" }}>
          {channel === "plan" ? "使用专属 Agent Plan 服务通道" : "使用按量付费服务通道"}（切换通道后保存即生效，无需重启）
        </div>
      )}
      <div className="grid three" style={{ gap: 10 }}>
        <Field label="生成随机度（0-2）">
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
        <Field label={`服务密钥（仅保存在服务端，不回显；当前：${view.readonly.llm_has_api_key ? "已配置" : "未配置"}）`}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <input style={{ ...inputStyle, flex: "1 1 240px", fontFamily: "monospace" }} type="password"
              value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="粘贴密钥后点击「保存密钥」" />
            <button className="btn primary sm" disabled={!apiKey.trim() || keyBusy}
              onClick={() => saveKey(apiKey)}>保存密钥</button>
            {view.readonly.llm_has_api_key && (
              <button className="btn sm" disabled={keyBusy} onClick={() => saveKey("")}>清除 Key</button>
            )}
          </div>
        </Field>
        <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 8 }}>
          密钥保存在运行服务所在服务器的本机环境配置文件中（.env），不写入应用日志，也不会回显到页面。
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
  const busy = isPending("cfg:platforms");

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(view.readonly.platforms)) {
      if (enabled[k] !== v.enabled) updates[`platforms.${k}.enabled`] = enabled[k];
    }
    if (Object.keys(updates).length === 0) { showToast("平台配置没有改动", "info"); return; }
    try {
      const res = await withPending("cfg:platforms", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✓ 已更新 ${res.changed.length} 项平台配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>平台</th><th style={{ width: 70 }}>启用</th><th>登录态</th></tr></thead>
          <tbody>
            {Object.entries(view.readonly.platforms).map(([k, v]) => (
              <tr key={k}>
                <td>{PLATFORM_LABELS[k] ?? k}</td>
                <td>
                  <input type="checkbox" checked={enabled[k] ?? false}
                    onChange={(e) => setEnabled((prev) => ({ ...prev, [k]: e.target.checked }))} />
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
        hint="至少保留 1 个平台；启用 ≠ 可发布，还需在「账号与登录」扫码登录" />
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
      showToast(`✓ 已保存 ${res.changed.length} 项高级参数`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
          三模式评分权重
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
        ▣ 两条安全门（维度匹配 / 可发布性）在代码层硬锁，任何配置都无法关闭；这里改的是词库与阈值，不是开关。
      </div>

      <SaveBar onSave={save} busy={busy} hint="保存后立即生效并" />
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
      showToast(`✓ 已保存 ${res.changed.length} 项超时配置`, "success");
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
        hint="producing 超时必须小于待发布超时；各阶段独立计时，不互相覆盖" />
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
      showToast(`✓ 已更新 ${res.changed.length} 个通知通道`, "success");
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
                        ? <span className="tag green">已配置</span>
                        : <span className="tag orange">未配置（.env）</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <SaveBar onSave={save} busy={busy}
        hint="启用只是允许发送；Webhook 地址只保存在本机配置文件中" />
    </>
  );
}

// =====================================================================
// 平台文案风格（platform_styles 表）：标题风格 / 正文结构 / 语气 / 话题标签
// =====================================================================
type StyleDraft = { title_style: string; body_style: string; tone: string; hashtags: string };

export function PlatformStyleEditor() {
  const { showToast, withPending, isPending } = useApp();
  const [styles, setStyles] = useState<PlatformStyle[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, StyleDraft>>({});
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const res = await api.platformStyles();
      setStyles(res.items);
      const next: Record<string, StyleDraft> = {};
      for (const s of res.items) {
        next[s.platform] = {
          title_style: s.title_style,
          body_style: s.body_style,
          tone: s.tone,
          hashtags: s.hashtags.join(", "),
        };
      }
      setDrafts(next);
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => { load(); }, []);

  /** "效率, 成长" → ["效率","成长"]；标签统一去掉 # 前缀与重复项 */
  const parseTags = (text: string): string[] => {
    const out: string[] = [];
    for (const raw of text.split(/[,，\n]/)) {
      const tag = raw.trim().replace(/^#+/, "");
      if (tag && !out.includes(tag)) out.push(tag);
    }
    return out;
  };

  const save = async (platform: Platform) => {
    const cur = styles?.find((s) => s.platform === platform);
    const d = drafts[platform];
    if (!cur || !d) return;
    const updates: Partial<Pick<PlatformStyle, "title_style" | "body_style" | "tone" | "hashtags">> = {};
    if (d.title_style.trim() !== cur.title_style) updates.title_style = d.title_style.trim();
    if (d.body_style.trim() !== cur.body_style) updates.body_style = d.body_style.trim();
    if (d.tone.trim() !== cur.tone) updates.tone = d.tone.trim();
    const tags = parseTags(d.hashtags);
    if (JSON.stringify(tags) !== JSON.stringify(cur.hashtags)) updates.hashtags = tags;

    if (Object.keys(updates).length === 0) { showToast("没有改动", "info"); return; }
    const pendingKey = `platform_style:${platform}`;
    try {
      const res = await withPending(pendingKey, () => api.updatePlatformStyle(platform, updates));
      if (!res) return;
      showToast(`✓ ${PLATFORM_LABELS[platform] ?? platform} 已保存 ${res.changed} 项`, "success");
      await load();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  if (loadErr) {
    return (
      <div className="note" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
        <span>加载失败: {loadErr}</span>
        <button className="btn sm" onClick={load}>↻ 重试</button>
      </div>
    );
  }
  if (styles === null) return <div className="note">加载中…</div>;
  if (styles.length === 0) {
    return (
      <div className="note">
        暂无平台风格记录，请重启后端完成数据库升级。
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="note" style={{ fontSize: 12 }}>
        平台文案风格会在生成内容时实时读取，大模型生成与模板降级使用同一套设置。
        话题标签留空时，只使用选题所属方向的标签。
      </div>
      {styles.map((s) => {
        const d = drafts[s.platform];
        if (!d) return null;
        const busy = isPending(`platform_style:${s.platform}`);
        return (
          <div key={s.platform} style={{ padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <strong style={{ fontSize: 13 }}>{PLATFORM_LABELS[s.platform] ?? s.platform}</strong>
              <button className="btn primary sm" disabled={busy} onClick={() => save(s.platform)}>
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
            <div className="grid three" style={{ gap: 10 }}>
              <Field label="标题风格">
                <input style={inputStyle} value={d.title_style}
                  onChange={(e) => setDrafts((p) => ({ ...p, [s.platform]: { ...p[s.platform], title_style: e.target.value } }))} />
              </Field>
              <Field label="正文结构">
                <input style={inputStyle} value={d.body_style}
                  onChange={(e) => setDrafts((p) => ({ ...p, [s.platform]: { ...p[s.platform], body_style: e.target.value } }))} />
              </Field>
              <Field label="语气">
                <input style={inputStyle} value={d.tone}
                  onChange={(e) => setDrafts((p) => ({ ...p, [s.platform]: { ...p[s.platform], tone: e.target.value } }))} />
              </Field>
            </div>
            <div style={{ marginTop: 10 }}>
              <Field label="话题标签（逗号分隔，不带 #；留空 = 只用选题维度标签）">
                <input style={inputStyle} value={d.hashtags} placeholder="如：效率提升, 职场成长, 实用清单"
                  onChange={(e) => setDrafts((p) => ({ ...p, [s.platform]: { ...p[s.platform], hashtags: e.target.value } }))} />
              </Field>
            </div>
            {s.updated_at && (
              <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 8 }}>
                最近更新 {s.updated_at}（{s.updated_by ?? "—"}）
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
