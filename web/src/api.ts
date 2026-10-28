// ===== API Client — 所有后端调用集中在这里 =====

// Platform 类型 + 元数据：唯一来源在 platforms.ts
import type { Platform as _Platform } from "./platforms";
export type Platform = _Platform;
export { platformMeta, PLATFORM_META, ALL_PLATFORMS } from "./platforms";

export interface RecoveryInfo {
  interrupted_tasks: number;
  requeued_data_fetch: string[];
  stale_rounds: number[];
  pending_verify: number;
}

export interface SystemStatus {
  enabled_platforms: Platform[];
  /** 有登录态的平台（R20 🔴 判定依据） */
  logged_in_platforms: Platform[];
  /** 无登录态的平台：真实操作为阻塞状态 */
  platforms_without_login: Platform[];
  circuit_broken_platforms: Platform[];
  active_rounds: number;
  published_samples: number;
  scoring_mode: string;
  recovery: RecoveryInfo;
}

export interface Goal {
  id: string;
  goal_text: string;
  target_metric: string;
  target_value: number;
  target_platform: Platform | null;
  metric_scope: string;
  baseline_value: number | null;
  baseline_at: string | null;
  metric_period_start: string | null;
  metric_period_end: string | null;
  data_source: string | null;
  current_value: number | null;
  progress_updated_at: string | null;
  /** 后端算好的达成率（%）；任一分量 NULL → null（R21） */
  achievement_rate: number | null;
  status: string;
  created_at: string;
}

export interface SpiralRound {
  id: number;
  goal_id: string | null;
  round_number: number;
  status: string;
  topic_ids: string;
  content_variants: string;
  plan_signature: string | null;
  created_at: string;
  awaiting_publish_ended_at: string | null;
  collecting_metrics_started_at: string | null;
}

export interface MetricsResponse {
  items: MetricRow[];
  total: number;
  z_stats: {
    platform: string; metric: string;
    mean: number; std: number; sample_count: number;
  }[];
  z_window_days: number;
  /** 可跨平台对比的比率指标（R13） */
  cross_platform_comparable: string[];
  /** 只允许平台内 z 的原始计数指标 */
  platform_only_metrics: string[];
}

export interface MetricRow {
  id?: number;
  platform: Platform;
  content_id: string | null;
  content_title?: string | null;
  round_id?: number | null;
  views: number | null;
  likes: number | null;
  collects: number | null;
  comments: number | null;
  shares: number | null;
  new_followers: number | null;
  interaction_rate: number | null;
  like_rate: number | null;
  collect_rate: number | null;
  fetch_status: string;
  fetched_at: string | null;
  attempt?: number | null;
  /** P1-8: 采集失败原因（来自 metrics.error_msg，failed/parse_error 时有值） */
  error_msg?: string | null;
  /** 平台内部 z-score；样本不足或原值为 NULL 时为 null（R3） */
  z: Record<string, number | null>;
}

export interface Draft {
  id: string;
  content_id: string;
  platform: Platform;
  status: string;
  saved_at: string | null;
  user_confirmed_at: string | null;
  error_msg: string | null;
  created_at: string;
  /** LEFT JOIN contents 带出，列表接口可能为空 */
  title?: string | null;
}

/** POST /api/rounds/{id}/run 的执行报告（RoundExecutor.run 返回） */
export interface RunRoundReport {
  round_id: number;
  round_number: number;
  started_at: string;
  finished_at?: string;
  platforms: Platform[];
  topic_count_requested: number;
  steps: { step: string; [key: string]: unknown }[];
  contents_created: number;
  drafts_created: number;
  drafts_saved: number;
  drafts_failed: number;
  guard_rejected: number;
  retry_counts?: Record<string, number>;
  errors: string[];
  final_status: string | null;
  plan_signature?: string;
}

export interface ScoredTopic {
  id: string;
  title: string;
  dimension: string;
  total_score: number;
  is_exploration: boolean;
  breakdown: Record<string, { raw: number; weight: number; score: number }>;
}

export interface GapSuggestion {
  dimension: string;
  gap_type: string;
  audience: string;
  suggestion_text: string;
  current_count: number;
  target_count: number | null;
  sample_count: number;
  avg_interaction_rate: number | null;
  confidence: string;
}

export interface SpiralAnalysis {
  round_id: number;
  gaps: GapSuggestion[];
  strategy_adjustments: { type: string; description: string }[];
  ops_adjustments: string[];
  evidence: Record<string, unknown>;
  /** G-04 单平台纵深（第四通道） */
  platform_breakdown?: PlatformInsight[];
}

// G-04 单平台纵深分析结果（数字可为 null：样本不足 ≠ 0）
export interface PlatformInsight {
  platform: Platform;
  round_id: number | null;
  days_analyzed: number;
  sample_count: number;
  avg_interaction_rate: number | null;
  p25_interaction_rate: number | null;
  p50_interaction_rate: number | null;
  p75_interaction_rate: number | null;
  top_dimensions: { dimension: string; sample_count: number; avg_interaction_rate: number | null }[];
  best_hours: { hour: number; sample_count: number; avg_interaction_rate: number | null }[];
  best_title_style: { avg_title_len?: number | null; best_bucket?: string; buckets?: { title_len: string; sample_count: number; avg_interaction_rate: number | null }[] };
  best_card_style: { card_style: string; sample_count: number; avg_interaction_rate: number | null } | null;
  conclusion: string;
}

export interface CircuitBreakerEvent {
  id: number;
  platform: Platform;
  reason: string;
  detected_at: string;
  cooldown_until: string | null;
  resolved_at: string | null;
  is_broken?: boolean;
}

// 热点雷达（R22：采集失败不给数字）
export interface HotTopicItem {
  id: string;
  title: string;
  source: string;
  discovered_at: string;
  url: string | null;
  heat: number | null;
  heat_unit: string | null;
  time_sensitive: number | null;
  evidence_status: "collected" | "demo" | "failed";
  dimension: string | null;
  dimension_match: boolean | null;
  dimension_hits: string[];
  publishable: boolean;
  redline_hits: string[];
}

/** /api/insights 返回的每平台复盘洞察 */
export interface InsightItem {
  id: number;
  round_id: number;
  platform: Platform;
  analyzed_at: string;
  days_analyzed: number;
  trend: { avg: number | null; p25: number | null; p50: number | null; p75: number | null };
  best_hours: number[];
  best_title_style: Record<string, unknown>;
  best_card_style: string | null;
  top_dimensions: string[];
  sample_count: number;
  avg_interaction_rate: number | null;
  conclusion: string;
}

export interface HotSourceStatus {
  platform: string;
  source: string;
  status: "collected" | "demo" | "failed";
  message: string | null;
  discovered_at: string | null;
}

export interface HotTopicsResponse {
  enabled: boolean;
  cached: boolean;
  refreshed_at: string | null;
  refresh_interval_minutes?: number;
  sources: HotSourceStatus[];
  items: HotTopicItem[];
  filters?: Record<string, unknown>;
  message?: string;
}

export interface ScheduleEvent {
  id: string;
  title: string;
  platform: Platform;
  status: string;
  round_id: number | null;
  scheduled: string | null;
  saved_at: string | null;
  day: number | null;
}

export interface ScheduleResponse {
  start_date: string;
  end_date: string;
  items: ScheduleEvent[];
  /** 未排期草稿（scheduled_publish_at IS NULL），供日历直接挑选排期 */
  unscheduled?: UnscheduledDraft[];
}

export interface UnscheduledDraft {
  id: string;
  platform: Platform;
  status: string;
  title: string | null;
  round_id: number | null;
}

/** G-13 内容资产（外部参考 / 个人观点 / 内容模板） */
export type AssetType = "reference" | "voice" | "template";

export interface AssetItem {
  id: number;
  asset_type: AssetType;
  title: string;
  body: string | null;
  source_url: string | null;
  platform: Platform | null;
  created_at: string | null;
  updated_at: string | null;
}

/** G-13 / G-24 运行时配置（白名单可写字段 + R22 硬锁 + 审计） */
export interface SystemConfigView {
  editable: {
    daily_quotas: Record<string, { passive_replies: number | null; active_comments: number | null; follows: number | null }>;
    ops_window: { start_hour: number | null; end_hour: number | null };
    cooldown_minutes: number | null;
    hot_topics: {
      heat_threshold: number | null;
      heat_filter_enabled: boolean | null;
      time_window_hours: number | null;
      time_filter_enabled: boolean | null;
    };
  };
  /** R22 两道硬锁：只读，后端拒绝关闭 */
  locked: { dimension_match_required: boolean; publishability_required: boolean };
  /** 账号画像 + KB + LLM + 平台连接（全只读，来自 config.yaml 解析结果） */
  readonly: {
    persona_keywords: string[];
    forbidden_directions: string[];
    redline_keywords_count: number;
    // KB 配置
    kb_path: string;
    kb_type: string;
    kb_table: string;
    kb_field_mapping: Record<string, string>;
    // LLM 配置
    llm_provider: string;
    llm_model: string;
    llm_fallback_model: string;
    llm_has_api_key: boolean;
    // 平台连接
    platforms: Record<string, { enabled: boolean; account_key: string; base_url: string }>;
  };
  overrides_file: string;
  overrides: Record<string, unknown>;
  audit: { id: number; key: string; old_value: string | null; new_value: string | null; changed_at: string | null }[];
}

/** G-13 选题库 schema 检查 */
export interface KbInspect {
  path: string;
  file_type: string;
  table?: string;
  field_mapping: Record<string, string>;
  columns: string[];
  missing_fields: string[];
  stats: { total?: number; unique_dimensions?: number };
  ok: boolean;
  error?: string;
}

// ===== G-09 母计划 / 内容编辑 / 生产线 =====
export interface MasterPlan {
  topic_count_per_platform: number;
  dimension_allocation: Record<string, number>;
  enabled_platforms: string[];
  content_format: string;
}

export interface PlanSignatureHistory {
  plan_signature: string;
  plan_snapshot: MasterPlan | string;
  round_id: number | null;
  created_at: string;
  author: string | null;
}

export interface PlanResponse {
  plan: MasterPlan;
  signature: string;
  updated_at: string | null;
  /** plan_settings=用户保存过 / derived=从最近一轮推导 */
  source: "plan_settings" | "derived" | string;
  per_platform: Record<Platform, { enabled: boolean; config_enabled: boolean; topic_count: number }>;
  history: PlanSignatureHistory[];
  /** R18：签名与当前母计划不一致的活跃轮次 */
  stale_rounds: { id: number; round_number: number; status: string; plan_signature: string }[];
}

export interface SavePlanResult {
  signature: string;
  previous_signature: string;
  changed: boolean;
  updated_at: string;
  plan: MasterPlan;
  stale_rounds: number[];
}

export interface ContentItem {
  id: string;
  round_id: number | null;
  topic_id: string | null;
  platform: Platform;
  title: string;
  body: string;
  tags: string[];
  image_count: number;
  word_count: number | null;
  card_style: string | null;
  guard_outcome: string | null;
  guard_reason: string | null;
  created_at: string;
  draft_id: string | null;
  draft_status: string | null;
  draft_error: string | null;
  draft_saved_at: string | null;
}

export interface RecheckResult {
  id: string;
  result: "pass" | "warn" | "fail" | "error";
  passes: boolean;
  reason: string;
  redline_hits: string[];
}

export interface PipelineStage {
  key: string;
  label: string;
  count: number;
  note: string;
}

export interface PipelinePlatformRow {
  platform: Platform;
  contents: number;
  guard_pass: number;
  cards: number;
  draft_saved: number;
  draft_pending: number;
  draft_failed: number;
}

export interface PipelineResponse {
  round_id: number;
  round_number: number;
  status: string;
  stages: PipelineStage[];
  guard_rejections: number | null;
  drafts_by_status: Record<string, number>;
  per_platform: PipelinePlatformRow[];
}

// ===== P1 — AI 证据审计链 =====
export interface AiCallRecord {
  id: number;
  step: string;
  module?: string | null;
  model: string;
  prompt_version: string;
  algo_version?: string | null;
  input_digest?: string | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  latency_ms: number;
  duration_ms?: number | null;
  output_valid?: number | null;
  success: number;
  attempt?: number;
  called_at: string;
}

export interface UserOverrideRecord {
  id: number;
  round_id?: number | null;
  ai_call_id?: number | null;
  override_field: string | null;
  original_value: string | null;
  override_value: string | null;
  reason: string;
  overridden_by: string;
  overridden_at: string;
}

export interface RedlineRecord {
  id: string;
  round_id: number | null;
  platform: Platform;
  title: string;
  guard_outcome: "warn" | "fail" | "error" | string;
  guard_reason: string | null;
  created_at: string;
}

// ===== 运营控制台补充类型 =====
export interface OpsKpiResponse {
  today_ops: Record<string, Record<string, number>>;
  draft_status: Record<string, number>;
  circuit_breaker_active: { platform: Platform; reason: string; detected_at: string; cooldown_until: string | null }[];
  platform_stats: Record<Platform, { rounds: number; drafts_saved: number; drafts_published: number; circuit_broken: boolean }>;
  /** 后端回显的展示窗口（1/7/30/90），today_ops 实际是按该窗口聚合 */
  window_days?: number;
}

export interface OpsLogItem {
  id: number;
  round_id: number | null;
  platform: Platform;
  action_type: "passive_reply" | "active_comment" | "follow" | "browse";
  target_url: string | null;
  target_author: string | null;
  content: string | null;
  schedule_time: string | null;
  executed_at: string | null;
  status: "success" | "failed" | "skipped" | "circuit_broken";
  error_msg: string | null;
  rhythm_wait_seconds: number | null;
}

export interface TaskRunItem {
  id: number;
  task_type: string;
  biz_id: string | null;
  platform: Platform | null;
  status: "pending" | "running" | "interrupted" | "success" | "failed" | "skipped" | "pending_verify";
  attempt: number;
  max_attempts: number;
  started_at: string | null;
  finished_at: string | null;
  next_run_at: string | null;
  error_msg: string | null;
  created_at: string;
}

export interface NotificationItem {
  id: number;
  level: "info" | "warning" | "error" | "critical";
  type: string;
  platform: Platform | null;
  title: string;
  body: string | null;
  channel: string | null;
  sent_at: string;
}

export interface SchedulerJob {
  id: string;
  name: string;
  next_run_time: string | null;
  trigger: string;
}

export interface RhythmConfig {
  enabled_platforms: Platform[];
  quotas: Record<Platform, { passive_replies: number; active_comments: number; follows: number }>;
  posting_windows: Record<Platform, string[]>;
  min_interval_seconds: number;
}

// ===== fetch wrapper =====
const API = ""; // vite proxy handles prefix

async function fetchJson<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${text || res.statusText}`);
  }
  return res.json();
}

// ===== API Functions =====
export const api = {
  status: () => fetchJson<SystemStatus>("/api/status"),
  goals: () => fetchJson<Goal[]>("/api/goals"),
  createGoal: (data: Partial<Goal>) =>
    fetchJson<{ id: string; message: string }>("/api/goals", {
      method: "POST", body: JSON.stringify(data),
    }),
  refreshGoalProgress: () =>
    fetchJson<{ updated: Record<string, number | null>; goals: Goal[] }>(
      "/api/goals/progress/refresh", { method: "POST" },
    ),
  rounds: (status?: string) =>
    fetchJson<SpiralRound[]>(status ? `/api/rounds?status=${status}` : "/api/rounds"),
  round: (id: number) => fetchJson<SpiralRound & { drafts: Draft[] }>(`/api/rounds/${id}`),
  createRound: (data: { topic_count?: number; platforms?: Platform[]; goal_id?: string }) =>
    fetchJson<{ id: number; round_number: number; status: string }>("/api/rounds", {
      method: "POST", body: JSON.stringify(data),
    }),
  runRound: (id: number, data: { topic_count?: number; platforms?: Platform[]; save_drafts?: boolean } = {}) =>
    fetchJson<RunRoundReport>(`/api/rounds/${id}/run`, {
      method: "POST", body: JSON.stringify(data),
    }),
  transitionRound: (id: number, target_status: string, reason = "") =>
    fetchJson<{ round_id: number; new_status: string }>(`/api/rounds/${id}/transition`, {
      method: "POST", body: JSON.stringify({ target_status, reason }),
    }),
  drafts: (status?: string, platform?: Platform) => {
    const p = new URLSearchParams();
    if (status) p.set("status", status);
    if (platform) p.set("platform", platform);
    return fetchJson<Draft[]>(`/api/drafts?${p}`);
  },
  confirmPublish: (draftId: string) =>
    fetchJson<{ draft_id: string; status: string }>(`/api/drafts/${draftId}/publish_confirm`, {
      method: "POST",
    }),
  /** 「❌ 不发了」：draft_pending / draft_saved → cancelled */
  cancelDraft: (draftId: string) =>
    fetchJson<{ draft_id: string; status: string }>(`/api/drafts/${draftId}/cancel`, {
      method: "POST",
    }),
  scoreTopics: (limit = 20) =>
    fetchJson<{ mode: string; topics: ScoredTopic[] }>(`/api/topics/score?limit=${limit}`),
  hotTopics: (refresh = false) =>
    fetchJson<HotTopicsResponse>(`/api/hot_topics${refresh ? "?refresh=true" : ""}`),
  gaps: (roundId?: number) =>
    fetchJson<SpiralAnalysis>(roundId ? `/api/gaps?round_id=${roundId}` : "/api/gaps"),
  /** 复盘洞察（螺旋引擎输出，每平台纵深分析 + 结论） */
  insights: () =>
    fetchJson<{ items: InsightItem[] }>("/api/insights"),
  /** 复盘「采纳建议」：把缺口维度写回母计划（后端触发 R18 下游失效） */
  adoptGaps: (dimensions: string[]) =>
    fetchJson<{ signature: string; adopted_dimensions: string[]; stale_rounds: number[] }>(
      "/api/plan_adjustments/adopt",
      { method: "POST", body: JSON.stringify({ dimensions }) },
    ),
  /** 热点 → 选题库（R22：必须有 source + 原文链接） */
  importHotTopic: (payload: { title: string; source: string; url: string; dimension?: string }) =>
    fetchJson<{ id: string; imported: boolean; message?: string }>("/api/hot_topics/import", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  metrics: (platform?: Platform) =>
    fetchJson<MetricsResponse>(`/api/metrics${platform ? `?platform=${platform}` : ""}`),
  circuitBreaker: () => fetchJson<{ events: CircuitBreakerEvent[]; broken_platforms: Platform[] }>("/api/circuit_breaker"),
  recoverCb: (platform: Platform) =>
    fetchJson<any>(`/api/circuit_breaker/recover?platform=${platform}`, { method: "POST" }),
  resetCb: (platform: Platform) =>
    fetchJson<{ reset_count: number }>(`/api/circuit_breaker/reset?platform=${platform}`, { method: "POST" }),
  schedule: (startDate?: string, endDate?: string) => {
    const p = new URLSearchParams();
    if (startDate) p.set("start_date", startDate);
    if (endDate) p.set("end_date", endDate);
    return fetchJson<ScheduleResponse>(`/api/schedule?${p}`);
  },
  // === G-13 排期写入口 ===
  scheduleDraft: (draft_id: string, scheduled_publish_at: string) =>
    fetchJson<{ draft_id: string; scheduled_publish_at: string | null; deduped?: boolean }>(
      "/api/schedule", { method: "POST", body: JSON.stringify({ draft_id, scheduled_publish_at }) },
    ),
  /** scheduled = null → 取消排期 */
  updateSchedule: (draft_id: string, scheduled: string | null) =>
    fetchJson<{ draft_id: string; scheduled_publish_at: string | null }>(
      `/api/schedule/${draft_id}`, { method: "PATCH", body: JSON.stringify({ scheduled_publish_at: scheduled }) },
    ),
  // === G-13 内容资产 / 字段映射 ===
  assets: (asset_type?: AssetType) =>
    fetchJson<{ items: AssetItem[]; total: number }>(
      `/api/assets${asset_type ? `?asset_type=${asset_type}` : ""}`,
    ),
  createAsset: (data: { asset_type: AssetType; title: string; body?: string; source_url?: string; platform?: Platform }) =>
    fetchJson<AssetItem>("/api/assets", { method: "POST", body: JSON.stringify(data) }),
  kbInspect: () => fetchJson<KbInspect>("/api/kb/inspect"),
  // === G-13 / G-24 运行时配置 ===
  config: () => fetchJson<SystemConfigView>("/api/config"),
  patchConfig: (updates: Record<string, unknown>) =>
    fetchJson<{ changed: { key: string; old: unknown; new: unknown }[]; noop: boolean; config: SystemConfigView["editable"] }>(
      "/api/config", { method: "PATCH", body: JSON.stringify({ updates }) },
    ),
  // === 运营控制台 ===
  /** days = 展示窗口（1=今日，7/30/90）；与 R12 评分 90 天口径独立 */
  opsKpi: (days = 1) => fetchJson<OpsKpiResponse>(`/api/ops_kpi?days=${days}`),
  /** 全局运营软暂停（人工开关，与熔断无关） */
  opsPause: () =>
    fetchJson<{ paused: boolean; updated_at: string | null; updated_by: string | null }>("/api/ops/pause"),
  setOpsPause: (paused: boolean) =>
    fetchJson<{ paused: boolean }>("/api/ops/pause", {
      method: "POST",
      body: JSON.stringify({ paused }),
    }),
  /** 通知通道自检（不落 notifications 表，不泄露 webhook） */
  notifyTest: () =>
    fetchJson<{ results: Record<string, "ok" | "failed" | "disabled"> }>("/api/notify/test", {
      method: "POST",
    }),
  opsLogs: (platform?: Platform, action_type?: string, status?: string, limit = 100) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (platform) p.set("platform", platform);
    if (action_type) p.set("action_type", action_type);
    if (status) p.set("status", status);
    return fetchJson<{ items: OpsLogItem[]; total: number }>(`/api/ops_logs?${p}`);
  },
  taskRuns: (taskType?: string, status?: string, limit = 100) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (taskType) p.set("task_type", taskType);
    if (status) p.set("status", status);
    return fetchJson<TaskRunItem[]>(`/api/task_runs?${p}`);
  },
  notifications: (level?: string, limit = 50) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (level) p.set("level", level);
    return fetchJson<NotificationItem[]>(`/api/notifications?${p}`);
  },
  schedulerJobs: () => fetchJson<{ jobs: SchedulerJob[]; count: number; running: boolean }>("/api/scheduler/jobs"),
  /** R17：pending_verify 人工核实收口（sent=已发出 / not_sent=未发出） */
  resolveTaskRun: (id: number, action: "sent" | "not_sent") =>
    fetchJson<{ id: number; status: string }>(`/api/task_runs/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    }),
  /** 立即触发一次定时任务（§8.3 手动触发验证） */
  runSchedulerJob: (jobId: string) =>
    fetchJson<{ job_id: string; triggered_at: string }>(`/api/scheduler/jobs/${jobId}/run`, {
      method: "POST",
    }),
  rhythm: () => fetchJson<RhythmConfig>("/api/rhythm"),
  // === G-09 母计划 / 内容编辑 / 生产线 ===
  plans: () => fetchJson<PlanResponse>("/api/plans"),
  savePlan: (data: MasterPlan) =>
    fetchJson<SavePlanResult>("/api/plans", { method: "POST", body: JSON.stringify(data) }),
  contents: (roundId: number, platform?: Platform) => {
    const p = new URLSearchParams({ round_id: String(roundId) });
    if (platform) p.set("platform", platform);
    return fetchJson<{ round_id: number; items: ContentItem[] }>(`/api/contents?${p}`);
  },
  updateContent: (id: string, data: { title?: string; body?: string; tags?: string[]; reason?: string }) =>
    fetchJson<{ id: string; changed: boolean; title: string; body: string; tags: string[] }>(
      `/api/contents/${id}`, { method: "PATCH", body: JSON.stringify(data) },
    ),
  recheckContent: (id: string) =>
    fetchJson<RecheckResult>(`/api/contents/${id}/recheck`, { method: "POST" }),
  rejectContent: (id: string, reason: string) =>
    fetchJson<{ id: string; affected_drafts: number; message: string }>(
      `/api/contents/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) },
    ),
  pipeline: (roundId: number) =>
    fetchJson<PipelineResponse>(`/api/rounds/${roundId}/pipeline`),
  // === P1 — AI 证据审计链 ===
  aiCalls: (limit = 200, step?: string) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (step) p.set("step", step);
    return fetchJson<AiCallRecord[]>(`/api/ai_calls?${p}`);
  },
  userOverride: (limit = 200) =>
    fetchJson<UserOverrideRecord[]>(`/api/user_override?limit=${limit}`),
  redlineHistory: (limit = 200) =>
    fetchJson<RedlineRecord[]>(`/api/redline_history?limit=${limit}`),
};
