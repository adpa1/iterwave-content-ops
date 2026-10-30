import { useEffect, useMemo, useState } from "react";
import { api, type ConfigMetadataItem } from "../api";
import { useApp } from "../AppContext";

const GROUPS: { prefix: string; title: string; exact?: string[] }[] = [
  { prefix: "vision_ranking.", title: "视觉理解" },
  { prefix: "media_generation.", title: "图片生成" },
  { prefix: "media_quality.", title: "素材质量评估" },
  { prefix: "tenant_switch.", title: "账号组切换" },
  { prefix: "tenant_backup.", title: "备份策略" },
  { prefix: "hot_topic_matcher.", title: "热点匹配" },
  { prefix: "circuit_breaker.", title: "熔断策略", exact: [
    "circuit_breaker.cooldown_minutes",
    "circuit_breaker.recovery_failure_extension_minutes",
  ] },
  { prefix: "hot_topics.", title: "热点采集与趋势策略" },
  { prefix: "autogrow.", title: "自动闭环反馈策略" },
  { prefix: "copy_generation.", title: "文案生产策略" },
  { prefix: "data_retention.", title: "数据保留策略" },
  { prefix: "insights.", title: "平台分析策略" },
  { prefix: "plan_suggestion.", title: "目标拆分建议策略" },
  { prefix: "spiral.", title: "复盘缺口与运营建议策略" },
  { prefix: "notifications.events.", title: "通知事件开关" },
  { prefix: "scheduler.misfire_grace_minutes", title: "调度器策略", exact: ["scheduler.misfire_grace_minutes"] },
  { prefix: "topic_kb.remote_timeout_seconds", title: "远程选题库来源", exact: ["topic_kb.remote_timeout_seconds"] },
  { prefix: "timeouts.", title: "采集重试与轮次上限", exact: [
    "timeouts.metrics_retry_attempts",
    "timeouts.metrics_retry_interval_minutes",
    "timeouts.goal_max_rounds",
    "timeouts.round_min_interval_hours",
    "timeouts.login_timeout_seconds",
    "timeouts.login_export_timeout_seconds",
    "timeouts.recovery_retry_delay_minutes",
  ] },
  { prefix: "scoring.", title: "选题策略", exact: [
    "scoring.cold_start_min_published",
    "scoring.stable_dimension_min_samples",
    "scoring.stable_exploration_percent",
    "scoring.normalization_window_days",
    "scoring.history_normalization_scale",
    "scoring.freshness_window_days",
    "scoring.dimension_gap_ratio",
    "scoring.dimension_gap_score",
    "scoring.dimension_normal_score",
    "scoring.no_persona_match_score",
    "scoring.timeliness_unknown_score",
    "scoring.history_cold_start_score",
    "scoring.freshness_recent_use_penalty",
    "scoring.freshness_min_score",
    "scoring.freshness_unused_score",
  ] },
];

const LABELS: Record<string, string> = {
  base_url: "API 地址",
  model: "模型名称",
  timeout_seconds: "请求总超时（秒）",
  connect_timeout_seconds: "连接超时（秒）",
  candidate_limit: "候选素材数",
  max_image_bytes: "单张图片上限（字节）",
  max_body_chars: "正文截取长度",
  temperature: "温度",
  max_tokens: "最大输出 Token",
  max_response_bytes: "响应体上限（字节）",
  max_dimension: "单边像素上限",
  max_pixels: "总像素上限",
  resolution_weight: "分辨率权重",
  composition_weight: "构图权重",
  contrast_weight: "对比度权重",
  brightness_weight: "亮度权重",
  sharpness_weight: "清晰度权重",
  color_diversity_weight: "色彩丰富度权重",
  resolution_reference_pixels: "分辨率参考像素",
  portrait_ratio_min: "竖图比例下限",
  portrait_ratio_max: "竖图比例上限",
  square_ratio_max: "方图比例上限",
  landscape_ratio_max: "横图比例上限",
  brightness_min: "亮度下限",
  brightness_max: "亮度上限",
  brightness_penalty_scale: "亮度偏离扣分尺度",
  contrast_reference: "对比度参考值",
  sharpness_reference: "清晰度参考值",
  sharpness_warning_threshold: "模糊警告阈值",
  low_contrast_threshold: "低对比度阈值",
  low_resolution_pixels: "低分辨率阈值",
  low_color_count: "低色彩数量阈值",
  color_count_reference: "色彩数量参考值",
  composition_near_square_multiplier: "近方图构图系数",
  composition_wide_multiplier: "宽幅构图系数",
  composition_extreme_multiplier: "极端比例构图系数",
  sharpness_fallback_contrast_reference: "备用清晰度参考值",
  sampling_size: "图像采样边长",
  color_quantization_colors: "色彩量化数量",
  cooldown_minutes: "首次熔断冷却（分钟）",
  max_items_per_source: "每个来源采集上限",
  fetch_timeout_seconds: "热点源请求超时（秒）",
  max_concurrent_fetches: "热点采集最大并发数",
  trend_burst_multiplier: "爆发热度倍数",
  trend_cooled_ratio: "降温峰值比例",
  trend_sustained_ratio: "持续爆发峰值比例",
  trend_previous_drop_ratio: "较上轮降温比例",
  trend_min_seen_count: "趋势判定最少上榜次数",
  cross_platform_similarity: "跨平台去重相似度",
  length_prefilter_ratio: "标题长度预筛比例",
  recency_decay: "复盘轮次衰减系数",
  full_confidence_samples: "满置信样本数",
  coverage_gap_bias_ratio: "覆盖缺口加分比例",
  validation_gap_bias_ratio: "验证缺口加分比例",
  min_dimension_bias: "最小维度偏差分",
  max_reference_titles_per_platform: "每平台参考标题数",
  max_copy_hints_per_platform: "每平台提示条数上限",
  max_copy_hint_chars: "单条复盘提示长度",
  topic_summary_max_chars: "选题摘要截取长度",
  title_max_chars: "标题最大字数",
  body_min_chars: "正文最少字数",
  body_max_chars: "正文最多字数",
  tag_min_count: "标签最少数量",
  tag_max_count: "标签最多数量",
  persona_keyword_limit: "画像关键词提示数量",
  forbidden_direction_limit: "禁区方向提示数量",
  strategy_hint_max_count: "复盘提示最多条数",
  strategy_hint_max_chars: "单条提示最大字数",
  max_guard_retries: "合规检查最大重试次数",
  raw_response_days: "平台原始响应保留天数",
  screenshot_days: "截图保留天数",
  ops_logs_days: "运营日志保留天数",
  ai_calls_detail_days: "AI 调用明细保留天数",
  misfire_grace_minutes: "错过计划任务宽限期（分钟）",
  metrics_retry_attempts: "数据采集最大尝试次数",
  metrics_retry_interval_minutes: "数据采集重试间隔（分钟）",
  goal_max_rounds: "单个目标最大轮数",
  round_min_interval_hours: "两轮最小间隔（小时）",
  login_timeout_seconds: "平台扫码登录超时（秒）",
  login_export_timeout_seconds: "登录态导出超时（秒）",
  recovery_retry_delay_minutes: "重启恢复补登记延迟（分钟）",
  remote_timeout_seconds: "远程选题库请求超时（秒）",
  window_days: "分析统计窗口（天）",
  cache_max_age_minutes: "分析缓存有效期（分钟）",
  top_dimensions_limit: "最多展示题材维度数",
  top_hours_limit: "最多展示时段数",
  default_topic_count: "默认每平台选题数",
  default_period_days: "默认目标规划周期（天）",
  minimum_sample_count: "推算所需最少历史样本",
  max_topics_per_platform: "每平台每轮选题数上限",
  max_rounds: "目标规划轮数上限",
  default_interval_days: "默认轮间隔（天）",
  min_dimension_samples: "维度判断最少样本数",
  validation_gap_topic_min: "验证缺口建议选题下限",
  validation_gap_topic_max: "验证缺口建议选题上限",
  coverage_gap_ratio: "覆盖缺口库存比例阈值",
  coverage_performance_multiplier: "表现优于全局均值倍数",
  min_suggested_add: "覆盖缺口最少建议补充量",
  min_platform_samples: "平台建议最少有效样本",
  low_interaction_rate_threshold: "低互动率阈值",
  min_metrics_for_ops_adjustment: "运营建议最少样本量",
  min_rounds_for_systematic_adjustment: "系统性调整所需轮数",
  request_drain_timeout_seconds: "请求排空等待（秒）",
  scheduler_drain_timeout_seconds: "调度器排空等待（秒）",
  round_drain_timeout_seconds: "运行轮次排空等待（秒）",
  background_jobs_timeout_seconds: "后台任务等待（秒）",
  login_process_timeout_seconds: "登录进程停止等待（秒）",
  max_bytes: "备份容量上限（字节）",
  retention_count: "备份保留份数",
  match_threshold: "匹配阈值",
  keyword_score: "关键词命中分",
  batch_size: "单批处理数量",
  max_per_run: "单轮最大处理量",
  recovery_failure_extension_minutes: "恢复验证失败延长冷却（分钟）",
  cold_start_min_published: "冷启动最低已发布数",
  stable_dimension_min_samples: "维度稳定样本数",
  history_normalization_scale: "历史表现归一化系数",
  freshness_window_days: "新鲜度统计窗口（天）",
  dimension_gap_ratio: "维度缺口比例",
  dimension_gap_score: "缺口维度评分",
  dimension_normal_score: "普通维度评分",
  no_persona_match_score: "无画像匹配默认分",
  stable_exploration_percent: "稳定期探索比例（%）",
  timeliness_unknown_score: "无时间信息默认分",
  history_cold_start_score: "无历史表现默认分",
  freshness_recent_use_penalty: "近期使用扣分",
  freshness_min_score: "新鲜度最低分",
  freshness_unused_score: "窗口内未使用默认分",
};

const NOTIFICATION_EVENT_LABELS: Record<string, string> = {
  draft_saved: "草稿已保存",
  draft_failed: "草稿保存失败",
  circuit_broken: "平台熔断",
  login_expired: "登录态过期",
  goal_completed: "目标达成",
  data_fetch_failed: "数据拉取失败",
  round_completed: "轮次复盘完成",
  circuit_recovered: "熔断恢复",
  publish_confirmed: "发布确认",
  quota_exhausted: "运营配额用尽",
  round_timeout: "轮次超时",
};

function labelFor(item: ConfigMetadataItem): string {
  if (item.key.startsWith("notifications.events.")) {
    const eventName = item.key.split(".")[2];
    return `${NOTIFICATION_EVENT_LABELS[eventName] || eventName}通知`;
  }
  return LABELS[item.key.split(".").pop() || item.key] || item.key;
}

export default function RuntimePolicyEditor() {
  const { showToast } = useApp();
  const [items, setItems] = useState<ConfigMetadataItem[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const result = await api.configMetadata();
      const selected = result.items.filter((item) => item.editable && (
        GROUPS.some((group) => group.exact
          ? group.exact.includes(item.key)
          : group.prefix.endsWith(".")
            ? item.key.startsWith(group.prefix)
            : item.key === group.prefix)
      ));
      setItems(selected);
      setDrafts(Object.fromEntries(selected.map((item) => [item.key, String(item.value ?? "")])));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const grouped = useMemo(() => GROUPS.map((group) => ({
    ...group,
    items: items.filter((item) => group.exact
      ? group.exact.includes(item.key)
      : group.prefix.endsWith(".")
        ? item.key.startsWith(group.prefix)
        : item.key === group.prefix),
  })).filter((group) => group.items.length > 0), [items]);

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const item of items) {
      const raw = drafts[item.key] ?? "";
      const original = String(item.value ?? "");
      if (raw === original) continue;
      if (item.type === "boolean") {
        updates[item.key] = raw === "true";
      } else if (item.type === "integer" || item.type === "number") {
        if (!raw.trim() || !Number.isFinite(Number(raw))) {
          showToast(`${item.key} 必须是有效数字`, "error");
          return;
        }
        updates[item.key] = item.type === "integer" ? Math.trunc(Number(raw)) : Number(raw);
      } else {
        updates[item.key] = raw;
      }
    }
    if (!Object.keys(updates).length) {
      showToast("没有需要保存的修改", "info");
      return;
    }
    setBusy(true);
    try {
      await api.patchConfig(updates);
      await load();
      showToast(`已保存 ${Object.keys(updates).length} 项运行策略参数`, "success");
    } catch (e) {
      showToast(`保存失败：${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="note">运行策略参数加载中…</div>;
  if (error) return <div className="panel"><div className="note" style={{ color: "var(--red)" }}>{error}</div><button className="btn sm" onClick={load}>重试</button></div>;

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">运行策略参数</div>
            <div className="panel-sub">字段范围由后端配置元数据提供；硬安全上限仍由代码强制，配置只能收紧，不能放宽。</div>
          </div>
          <button className="btn primary sm" onClick={save} disabled={busy}>{busy ? "保存中…" : "保存全部修改"}</button>
        </div>
        {grouped.map((group) => (
          <section key={group.title} style={{ marginTop: 16 }}>
            <div className="section-title">{group.title}</div>
            <div className="grid two">
              {group.items.map((item) => {
                const numeric = item.type === "integer" || item.type === "number";
                const boolean = item.type === "boolean";
                return (
                  <div key={item.key}>
                    <label className="form-label" htmlFor={item.key}>{labelFor(item)}</label>
                    {boolean ? (
                      <input
                        id={item.key}
                        type="checkbox"
                        checked={drafts[item.key] === "true"}
                        onChange={(event) => setDrafts((prev) => ({ ...prev, [item.key]: String(event.target.checked) }))}
                      />
                    ) : (
                      <input
                        id={item.key}
                        className="field"
                        type={numeric ? "number" : "text"}
                        step={item.type === "integer" ? 1 : "any"}
                        min={item.min ?? undefined}
                        max={item.max ?? undefined}
                        value={drafts[item.key] ?? ""}
                        onChange={(event) => setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))}
                      />
                    )}
                    <div className="metric-foot">{item.key}{item.min != null || item.max != null ? ` · 范围 ${item.min ?? "不限"}–${item.max ?? "不限"}` : ""}</div>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
