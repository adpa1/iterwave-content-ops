import { useEffect, useState } from "react";
import EmptyHint from "../components/EmptyHint";
import { api, MetricsResponse, MetricRow, Platform, SpiralAnalysis, Draft, WeeklyReport, ActivationMetrics, ActivationStatus } from "../api";

const PLATFORM_NAME: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音",
  kuaishou: "快手", wechat_channels: "视频号",
};

/** R3：NULL 显示灰色「—」，0 显示正常 0 */
function Num({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span style={{ color: "var(--subtle)" }}>—</span>;
  return <>{value}</>;
}

/** R13：z-score 保留 2 位；NULL 灰「—」 */
function Z({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span style={{ color: "var(--subtle)" }}>—</span>;
  const color = value >= 1 ? "var(--green)" : value <= -1 ? "var(--red)" : "var(--text)";
  return <span style={{ color, fontWeight: 600 }}>{value > 0 ? "+" : ""}{value.toFixed(2)}</span>;
}

const METRIC_NAME: Record<string, string> = {
  interaction_rate: "互动率", like_rate: "点赞率", collect_rate: "收藏率", follower_efficiency: "涨粉效率",
  views: "曝光", likes: "点赞", collects: "收藏", comments: "评论", shares: "分享", new_followers: "新增粉丝",
};

export default function Analytics() {
  const [analysis, setAnalysis] = useState<SpiralAnalysis | null>(null);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [platformFilter, setPlatformFilter] = useState<Platform | "">("");
  const [gapsError, setGapsError] = useState<string | null>(null);
  const [adopting, setAdopting] = useState<string | null>(null);
  const [adoptMsg, setAdoptMsg] = useState<string | null>(null);
  const [publishedDrafts, setPublishedDrafts] = useState<Draft[]>([]);
  const [backfillBusy, setBackfillBusy] = useState<string | null>(null);
  const [backfillValues, setBackfillValues] = useState<Record<string, {
    note_url: string; measured_at: string; correction_of: string;
    views: string; likes: string; collects: string;
    comments: string; shares: string;
  }>>({});
  const [weeklyReport, setWeeklyReport] = useState<WeeklyReport | null>(null);
  const [weeklyActionBusy, setWeeklyActionBusy] = useState<string | null>(null);
  const [activationMetrics, setActivationMetrics] = useState<ActivationMetrics | null>(null);
  const [activationStatus, setActivationStatus] = useState<ActivationStatus | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [snapshotHistory, setSnapshotHistory] = useState<Record<string, {
    id: string; source: string; measured_at: string; recorded_at: string;
    views: number | null; likes: number | null; collects: number | null;
    comments: number | null; shares: number | null;
    correction_of: string | null;
  }[]>>({});
  const [historyOpen, setHistoryOpen] = useState<string | null>(null);
  const [detailView, setDetailView] = useState<"overview" | "details">("overview");

  const loadGaps = () => {
    setGapsError(null);
    api.gaps().then(setAnalysis).catch((e) => setGapsError(e.message));
  };
  /** R19：只有 coverage_gap（audience=user）可采纳；validation_gap 是 Agent 内部建议 */
  const adoptGap = async (dimension: string) => {
    setAdopting(dimension);
    setAdoptMsg(null);
    try {
      const r = await api.adoptGaps([dimension]);
      setAdoptMsg(`已把「${dimension}」写回母计划（影响 ${(r.stale_rounds ?? []).length} 个活跃轮次）`);
      loadGaps();
    } catch (e: any) {
      setAdoptMsg(`采纳失败: ${e.message}`);
    } finally {
      setAdopting(null);
    }
  };
  const updateWeeklyAction = async (
    suggestionKey: string,
    decision: "adopted" | "ignored",
    outcome?: "success" | "no_effect" | "unclear",
  ) => {
    setWeeklyActionBusy(suggestionKey);
    setReportError(null);
    try {
      await api.weeklyActionFeedback({ suggestion_key: suggestionKey, decision, outcome });
      setWeeklyReport(await api.weeklyReport());
    } catch (e: any) {
      setReportError(e?.message || "建议反馈保存失败");
    } finally {
      setWeeklyActionBusy(null);
    }
  };

  const loadMetrics = (p: Platform | "") => {
    setMetricsError(null);
    api.metrics(p || undefined).then(setMetrics).catch((e) => setMetricsError(e.message));
  };

  useEffect(() => { loadGaps(); }, []);
  useEffect(() => { loadMetrics(platformFilter); }, [platformFilter]);
  useEffect(() => {
    Promise.all([api.drafts(), api.weeklyReport()]).then(async ([allDrafts, report]) => {
      // 先建立/读取 cohort，再计算 activation 指标，避免首次进入 Analytics 时发生初始化竞态。
      const status = await api.activationStatus();
      const metrics = await api.activationMetrics();
      const published = allDrafts.filter((d) => !!d.user_confirmed_at);
      setPublishedDrafts(published);
      setWeeklyReport(report);
      setActivationMetrics(metrics);
      setActivationStatus(status);
      setBackfillValues(Object.fromEntries(published.map((d) => [d.id, {
        note_url: d.platform_note_url || "", measured_at: "", correction_of: "",
        views: "", likes: "", collects: "", comments: "", shares: "",
      }])));
    }).catch((e) => setReportError(e?.message || "发布回填与周报数据加载失败"));
  }, []);

  const toggleSnapshotHistory = async (draftId: string) => {
    if (historyOpen === draftId) { setHistoryOpen(null); return; }
    setHistoryOpen(draftId);
    if (snapshotHistory[draftId]) return;
    try {
      const result = await api.metricSnapshots(draftId);
      setSnapshotHistory((prev) => ({ ...prev, [draftId]: result.items }));
    } catch (e: any) {
      setReportError(e?.message || "历史快照加载失败");
    }
  };

  const saveBackfill = async (draft: Draft) => {
    const value = backfillValues[draft.id];
    if (!value) return;
    setBackfillBusy(draft.id);
    setReportError(null);
    try {
      const numeric = ["views", "likes", "collects", "comments", "shares"] as const;
      const metrics: {
        views?: number | null; likes?: number | null; collects?: number | null;
        comments?: number | null; shares?: number | null;
      } = {};
      for (const key of numeric) {
        const raw = value[key].trim();
        if (!raw) metrics[key] = null;
        else {
          const n = Number(raw);
          if (!Number.isInteger(n) || n < 0) throw new Error(`${key} 必须填写非负整数`);
          metrics[key] = n;
        }
      }
      if (value.note_url.trim() !== (draft.platform_note_url || "")) {
        await api.updateDraftNoteUrl(draft.id, value.note_url.trim());
      }
      const metricsSubmitted = numeric.some((key) => value[key].trim());
      if (metricsSubmitted) {
        await api.submitManualMetrics(draft.id, {
          ...metrics,
          measured_at: value.measured_at || undefined,
          correction_of: value.correction_of || null,
        });
      }
      if (metricsSubmitted) {
        const [report, activation] = await Promise.all([api.weeklyReport(), api.activationMetrics()]);
        setWeeklyReport(report);
        setActivationMetrics(activation);
        setSnapshotHistory((prev) => {
          const next = { ...prev };
          delete next[draft.id];
          return next;
        });
      }
      const refreshed = await api.drafts();
      setPublishedDrafts(refreshed.filter((d) => !!d.user_confirmed_at));
      setBackfillValues((prev) => ({ ...prev, [draft.id]: {
        ...prev[draft.id], views: "", likes: "", collects: "", comments: "", shares: "",
        correction_of: "",
      }}));
      setWeeklyReport(await api.weeklyReport());
      setActivationMetrics(await api.activationMetrics());
    } catch (e: any) {
      setReportError(e?.message || "保存失败");
    } finally {
      setBackfillBusy(null);
    }
  };

  const rows: MetricRow[] = metrics?.items ?? [];
  const zStats = metrics?.z_stats ?? [];
  const adjustments = analysis?.strategy_adjustments ?? [];
  const rateRows = rows.filter((r) => typeof r.interaction_rate === "number");
  const rateCount = rateRows.length;
  const avgRate = rateCount ? rateRows.reduce((s, r) => s + (r.interaction_rate as number), 0) / rateCount : null;
  const zPlatforms = new Set(zStats.filter((z) => z.sample_count >= 2).map((z) => z.platform)).size;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">数据复盘</div>
          <h1 className="page-title">数据分析</h1>
          <div className="page-desc">
            总览看整体表现与本周建议；详细数据查看逐篇原始指标、平台基准和发布后数据回填。 当前视图：{detailView === "overview" ? "经营摘要" : "逐篇数据与回填"}.
          </div>
        </div>
        <div className="head-actions">
          <button className={detailView === "overview" ? "btn primary" : "btn"} onClick={() => setDetailView("overview")}>总览</button>
          <button className={detailView === "details" ? "btn primary" : "btn"} onClick={() => setDetailView("details")}>详细数据</button>
          <button className="btn" onClick={() => { loadMetrics(platformFilter); loadGaps(); }}>↻ 刷新数据</button>
        </div>
      </div>

      {detailView === "overview" && <>
      <div className="grid kpis" style={{ marginBottom: 15 }}>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">已采集内容</span><span className="metric-icon" aria-hidden="true">▤</span></div>
          <div className="metric-val">{metrics ? metrics.total : "—"} <span className="metric-unit">篇</span></div>
          <div className="metric-foot">近 {metrics?.z_window_days ?? 90} 天发布的内容</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">平均互动率</span><span className="metric-icon" aria-hidden="true">↗</span></div>
          <div className="metric-val">{avgRate == null ? "—" : `${(avgRate * 100).toFixed(2)}%`}</div>
          <div className="metric-foot">{avgRate == null ? "暂无有效样本" : `基于 ${rateCount} 篇有效样本`}</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">可做对比的平台</span><span className="metric-icon" aria-hidden="true">◎</span></div>
          <div className="metric-val">{zPlatforms} <span className="metric-unit">个</span></div>
          <div className="metric-foot">至少 2 篇有效样本才参与平台内对比</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">复盘建议</span><span className="metric-icon" aria-hidden="true">✦</span></div>
          <div className="metric-val">{adjustments.length} <span className="metric-unit">条</span></div>
          <div className="metric-foot">来自最近一轮复盘</div>
        </div>
      </div>

      <div className="grid kpis" style={{ marginBottom: 15 }}>
        <div className="metric"><div className="metric-label">首次价值时间 TTV</div><div className="metric-val">{activationMetrics?.ttv_seconds == null ? "—" : `${Math.round(activationMetrics.ttv_seconds)} 秒`}</div><div className="metric-foot">输入 {activationMetrics?.activation_input_wait_seconds == null ? "—" : Math.round(activationMetrics.activation_input_wait_seconds)} 秒 · 排队 {activationMetrics?.model_queue_wait_seconds == null ? "—" : Math.round(activationMetrics.model_queue_wait_seconds)} 秒 · 生成 {activationMetrics?.generation_processing_seconds == null ? "—" : Math.round(activationMetrics.generation_processing_seconds)} 秒</div><div className="metric-foot">P50 {activationMetrics?.ttv_p50_seconds == null ? "—" : Math.round(activationMetrics.ttv_p50_seconds)} 秒 · P90 {activationMetrics?.ttv_p90_seconds == null ? "—" : Math.round(activationMetrics.ttv_p90_seconds)} 秒 · 样本 {activationMetrics?.ttv_sample_count ?? 0}</div><div className="metric-foot">{activationMetrics?.ttv_percentile_scope || "分位数范围待定"}</div></div>
        <div className="metric"><div className="metric-label">7 日激活率</div><div className="metric-val">{activationMetrics?.seven_day_activation_rate == null ? "观察中" : `${(activationMetrics.seven_day_activation_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">复制或保存至少一篇合格样稿</div></div>
        <div className="metric"><div className="metric-label">首次发布率</div><div className="metric-val">{activationMetrics?.first_publish_rate == null ? "观察中" : `${(activationMetrics.first_publish_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">初始化后 7 日内确认发布</div></div>
        <div className="metric"><div className="metric-label">数据回流完成率</div><div className="metric-val">{activationMetrics?.data_return_rate == null ? "—" : `${(activationMetrics.data_return_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">发布满 7 天且 7 日内补填链接或提交有效指标</div></div>
        <div className="metric"><div className="metric-label">快速体验完成率</div><div className="metric-val">{activationMetrics?.quick_experience_completion_rate == null ? "—" : `${(activationMetrics.quick_experience_completion_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">成功展示三篇合格样稿的账号组</div></div>
        <div className="metric"><div className="metric-label">周采纳发布数</div><div className="metric-val">{activationMetrics?.weekly_adopted_published ?? 0}</div><div className="metric-foot">用户确认采纳并发布的内容</div></div>
        <div className="metric"><div className="metric-label">周有效复盘数</div><div className="metric-val">{activationMetrics?.weekly_effective_review_count ?? 0}</div><div className="metric-foot">近 7 天有有效指标的已发布内容</div></div>
      </div>
      {activationMetrics?.platform_model_breakdown?.length ? <div className="panel-sub" style={{ marginBottom: 15 }}>
        体验分布：{activationMetrics.platform_model_breakdown.map((item) => `${PLATFORM_NAME[item.platform]} / ${item.model_mode}：${item.ready_count}/${item.run_count} 完成`).join("；")}
      </div> : null}
      <div className="panel" style={{ marginBottom: 15 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title">互动表现较好的内容</div>
            <div className="panel-sub">按近 {metrics?.z_window_days ?? 90} 天有效互动率排序；仅展示有有效指标的内容</div>
          </div>
        </div>
        {rateRows.length === 0 ? <EmptyHint next="先在详细数据中为已发布内容补填数据，或等待平台采集完成">暂无可比较的互动率样本</EmptyHint> : (() => {
          const topRows = [...rateRows].sort((x, y) => (y.interaction_rate ?? 0) - (x.interaction_rate ?? 0)).slice(0, 8);
          const maxRate = Math.max(...topRows.map((r) => r.interaction_rate ?? 0), 0.0001);
          return <div style={{ padding: "4px 16px 16px" }}>
            {topRows.map((item, index) => {
              const rate = item.interaction_rate ?? 0;
              return <div key={item.content_id ?? `${item.platform}-${index}`} style={{ display: "grid", gridTemplateColumns: "minmax(110px, 1.5fr) minmax(90px, 3fr) 64px", gap: 10, alignItems: "center", padding: "8px 0", borderBottom: "1px solid var(--line)" }}>
                <div style={{ minWidth: 0 }}>
                  <div className="task-title" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.content_title || "未命名内容"}</div>
                  <div className="task-desc">{PLATFORM_NAME[item.platform]}</div>
                </div>
                <div style={{ height: 9, background: "var(--surface-2)", borderRadius: 99, overflow: "hidden" }}>
                  <div style={{ width: `${Math.max(2, rate / maxRate * 100)}%`, height: "100%", background: "var(--blue)", borderRadius: 99 }} />
                </div>
                <div style={{ textAlign: "right", fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{(rate * 100).toFixed(2)}%</div>
              </div>;
            })}
          </div>;
        })()}
      </div>
      </>}

      {detailView === "details" && <>
      <div className="panel" style={{ marginBottom: 15 }}>
        <div className="panel-head"><div><div className="panel-title">发布后数据回填</div><div className="panel-sub">可补充发布链接和人工指标。每次提交保留独立快照，未填写字段保持未知。</div></div></div>
        {reportError && <div className="note" role="alert" style={{ color: "var(--red)", padding: 12 }}>{reportError}</div>}
        {publishedDrafts.length === 0 ? <EmptyHint next="在内容生产页确认一篇内容已发布后，可在这里补填链接与数据">暂无已确认发布的内容</EmptyHint> : (
          <div className="list" style={{ padding: "0 16px 16px" }}>
            {publishedDrafts.map((draft) => {
              const value = backfillValues[draft.id];
              if (!value) return null;
              return <div className="task-row" key={draft.id} style={{ display: "block", padding: "12px 0" }}>
                <div className="task-title">{draft.title || "未命名内容"} · {PLATFORM_NAME[draft.platform]}</div>
                <label className="form-label">发布链接（可稍后补填）</label>
                <input className="field" value={value.note_url} placeholder="https://…" onChange={(e) => setBackfillValues((p) => ({ ...p, [draft.id]: { ...p[draft.id], note_url: e.target.value } }))} />
                <label className="form-label" style={{ marginTop: 8 }}>数据统计时间</label>
                <input className="field" type="datetime-local" value={value.measured_at} onChange={(e) => setBackfillValues((p) => ({ ...p, [draft.id]: { ...p[draft.id], measured_at: e.target.value } }))} />
                {value.correction_of && <div className="panel-sub">当前录入将作为历史快照的修正记录保存，不覆盖原始数据。</div>}
                <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8, marginTop: 8 }}>
                  {(activationStatus?.platform_metric_fields?.[draft.platform] || ["views", "likes", "collects", "comments", "shares"]).map((key) => <label key={key} className="form-label">{({views:"曝光/播放",likes:"点赞",collects:"收藏",comments:"评论",shares:"分享"} as Record<string,string>)[key] || key}<input className="field" inputMode="numeric" value={value[key as keyof typeof value] as string} placeholder="留空=未知" onChange={(e) => setBackfillValues((p) => ({ ...p, [draft.id]: { ...p[draft.id], [key]: e.target.value } }))} /></label>)}
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                  <button className="btn sm primary" disabled={backfillBusy === draft.id} onClick={() => saveBackfill(draft)}>{backfillBusy === draft.id ? "保存中…" : "保存链接与指标"}</button>
                  <button className="btn sm" onClick={() => toggleSnapshotHistory(draft.id)}>{historyOpen === draft.id ? "收起历史" : "查看历史快照"}</button>
                </div>
                {historyOpen === draft.id && <div style={{ marginTop: 10, borderTop: "1px solid var(--line)", paddingTop: 10 }}>
                  {(snapshotHistory[draft.id] || []).length === 0 ? <div className="panel-sub">暂无历史指标快照</div> :
                    (snapshotHistory[draft.id] || []).map((item) => <div className="panel-sub" key={item.id} style={{ padding: "6px 0" }}>
                      {item.measured_at} · {item.source === "manual" ? "人工填写" : item.source === "platform_readback" ? "平台读回" : item.source}
                      · {(activationStatus?.platform_metric_fields?.[draft.platform] || []).map((field) => `${({views:"曝光",likes:"点赞",collects:"收藏",comments:"评论",shares:"分享"} as Record<string,string>)[field] || field} ${item[field as keyof typeof item] ?? "—"}`).join(" · ")}
                      {item.correction_of ? " · 修正记录" : ""}
                      {item.source === "manual" && <button type="button" className="btn sm" style={{ marginLeft: 8 }} onClick={() => setBackfillValues((prev) => ({
                        ...prev, [draft.id]: {
                          ...prev[draft.id], views: item.views == null ? "" : String(item.views),
                          likes: item.likes == null ? "" : String(item.likes),
                          collects: item.collects == null ? "" : String(item.collects),
                          comments: item.comments == null ? "" : String(item.comments),
                          shares: item.shares == null ? "" : String(item.shares),
                          measured_at: item.measured_at ? item.measured_at.replace(" ", "T").slice(0, 16) : "",
                          correction_of: item.id,
                        },
                      }))}>基于此记录修正</button>}
                    </div>)}
                </div>}
              </div>;
            })}
          </div>
        )}
      </div>

      </>}

      {detailView === "overview" && <>
      <div className="panel" style={{ marginBottom: 15 }}>
        <div className="panel-head"><div><div className="panel-title">本周轻量复盘</div><div className="panel-sub">同平台比较；展示数据来源与样本量，不把相关性解释成因果。</div></div></div>
        {!weeklyReport ? <div className="loading"><div className="spinner" />加载周报…</div> : <>
          <div style={{ padding: "0 16px 16px" }}>
            <div className="panel-sub">{weeklyReport.period.start} 至 {weeklyReport.period.end} · 有效样本 {weeklyReport.sample_size} 篇 · 可复盘 {weeklyReport.effective_review_count ?? 0} 篇 · 来源：{weeklyReport.data_sources.join("、") || "暂无"}</div>
            <h3 style={{ marginTop: 14 }}>表现最好</h3>
            {weeklyReport.best_content.length ? weeklyReport.best_content.map((item) => <div className="task-row" key={`${item.content_id}-${item.metric}`}><div className="task-main"><div className="task-title">{item.title}</div><div className="task-desc">{PLATFORM_NAME[item.platform]} · {item.metric}：{item.value} · {item.source} · 统计时间 {item.measured_at}{item.conflict ? " · 来源冲突，按规则选择" : ""}</div></div></div>) : <div className="panel-sub">暂无有效指标，暂不排名。</div>}
            {!!weeklyReport.data_conflicts?.length && <div className="note" style={{ marginTop: 12 }}>
              <strong>数据来源冲突（保留原始记录）</strong>
              {weeklyReport.data_conflicts.map((conflict) => <div className="panel-sub" key={conflict.content_id}>
                {PLATFORM_NAME[conflict.platform]} · 内容 {conflict.content_id.slice(0, 8)} · 指标：{conflict.metrics.join("、")}
              </div>)}
            </div>}
            <h3 style={{ marginTop: 14 }}>可能原因</h3>
            {weeklyReport.why_it_may_work.map((item, i) => <div key={i} className="task-row" style={{ display: "block" }}>
              <p className="panel-sub">{item.statement}（样本 {item.sample_size}）</p>
              <details>
                <summary className="form-label" style={{ cursor: "pointer" }}>查看证据与适用范围</summary>
                <div className="panel-sub">平台：{item.platform ? PLATFORM_NAME[item.platform] : "全部"} · 时间范围：{item.time_window ? item.time_window.start + " 至 " + item.time_window.end : "未指定"} · 数据来源：{weeklyReport.data_sources.join("、") || "暂无"}</div>
                <div className="panel-sub">证据内容 ID：{item.evidence_content_ids?.join("、") || "无"}</div>
                <div className="panel-sub">指标快照 ID：{item.metric_snapshot_ids?.join("、") || "无"}</div>
              </details>
            </div>)}
            <h3 style={{ marginTop: 14 }}>下一轮建议</h3>
            {weeklyReport.next_actions.length ? weeklyReport.next_actions.map((item, i) => <div key={item.suggestion_key || i} className="task-row" style={{ display: "block" }}>
              <div className="task-title">{item.action}</div>
              <div className="task-desc">依据：{item.basis} · 验证：{item.verification}</div>
              <details><summary className="form-label" style={{ cursor: "pointer" }}>查看建议证据</summary>
                <div className="panel-sub">平台：{item.platform ? PLATFORM_NAME[item.platform] : "全部"} · 样本：{item.sample_size} · 时间范围：{item.time_window ? item.time_window.start + " 至 " + item.time_window.end : "未指定"}</div>
                <div className="panel-sub">证据内容 ID：{item.evidence_content_ids?.join("、") || "无"}</div>
                <div className="panel-sub">指标快照 ID：{item.metric_snapshot_ids?.join("、") || "无"}</div>
              </details>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10, alignItems: "center" }}>
                <span className="tag">{item.decision === "adopted" ? "已采纳" : item.decision === "ignored" ? "已忽略" : "待处理"}</span>
                {item.decision === "pending" && <>
                  <button className="btn sm primary" disabled={weeklyActionBusy === item.suggestion_key}
                    onClick={() => updateWeeklyAction(item.suggestion_key, "adopted")}>采纳建议</button>
                  <button className="btn sm" disabled={weeklyActionBusy === item.suggestion_key}
                    onClick={() => updateWeeklyAction(item.suggestion_key, "ignored")}>忽略</button>
                </>}
                {item.decision === "adopted" && <>
                  <span className="panel-sub">验证结果：</span>
                  <button className="btn sm" disabled={weeklyActionBusy === item.suggestion_key}
                    onClick={() => updateWeeklyAction(item.suggestion_key, "adopted", "success")}>有效</button>
                  <button className="btn sm" disabled={weeklyActionBusy === item.suggestion_key}
                    onClick={() => updateWeeklyAction(item.suggestion_key, "adopted", "no_effect")}>无明显效果</button>
                  <button className="btn sm" disabled={weeklyActionBusy === item.suggestion_key}
                    onClick={() => updateWeeklyAction(item.suggestion_key, "adopted", "unclear")}>暂无法判断</button>
                  {item.outcome !== "pending" && <span className="panel-sub">已记录：{item.outcome === "success" ? "有效" : item.outcome === "no_effect" ? "无明显效果" : "暂无法判断"}</span>}
                </>}
              </div>
            </div>) : <div className="panel-sub">样本不足，先补充发布数据，再生成策略建议。</div>}
            <div className="panel-sub" style={{ marginTop: 10 }}>限制：{weeklyReport.limitations.join("；")}</div>
          </div>
        </>}
      </div>

      </>}

      {detailView === "details" && <>
      {/* G-08 逐篇数据 */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">σ</span>逐篇数据</div>
            <div className="panel-sub">
              上方为平台读回的原始值，下方为和本平台平均水平的差距。
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button className={`btn sm ${platformFilter === "" ? "primary" : ""}`} onClick={() => setPlatformFilter("")}>全部平台</button>
            {(Object.keys(PLATFORM_NAME) as Platform[]).map((p) => (
              <button key={p} className={`btn sm ${platformFilter === p ? "primary" : ""}`} onClick={() => setPlatformFilter(p)}>
                {PLATFORM_NAME[p]}
              </button>
            ))}
          </div>
        </div>

        {metricsError && (
          <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
            <span>! 数据加载失败：{metricsError}</span>
            <button className="btn sm" onClick={() => loadMetrics(platformFilter)}>重试</button>
          </div>
        )}
        {!metrics && !metricsError && <div className="loading"><div className="spinner" />加载中...</div>}

        {metrics && rows.length === 0 && (
          <EmptyHint next="运行一轮 → 手动发布 → 24h 后自动采集">暂无已采集数据</EmptyHint>
        )}

        {metrics && rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>平台</th><th>内容</th><th>采集状态</th>
                  <th>失败原因</th>
                  <th>曝光</th><th>点赞</th><th>收藏</th><th>评论</th>
                  <th>互动率</th><th>点赞率</th><th>收藏率</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m, i) => (
                  <tr key={m.id ?? i}>
                    <td>{PLATFORM_NAME[m.platform] ?? m.platform}</td>
                    <td style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {m.content_title || <span style={{ color: "var(--subtle)" }}>—</span>}
                    </td>
                    {/* P1-7 修复：五态颜色分别映射，pending 中性 blue 不显示红色 */}
                    <td>
                      <span className={`tag ${
                        m.fetch_status === "success" ? "green" :
                        m.fetch_status === "partial" ? "orange" :
                        m.fetch_status === "failed" ? "red" :
                        m.fetch_status === "parse_error" ? "red" :
                        "gray"  // pending / 其他：中性色
                      }`}>{({ success: "已采集", partial: "部分采集", failed: "失败", parse_error: "解析失败", pending: "待采集" } as Record<string, string>)[m.fetch_status] ?? m.fetch_status}</span>
                    </td>
                    {/* P1-8 修复：失败态显示 error_msg（来自 metrics.error_msg 字段） */}
                    <td style={{ maxWidth: 180, fontSize: 11, color: (m.fetch_status === "failed" || m.fetch_status === "parse_error") ? "var(--red)" : "var(--subtle)" }}>
                      {(m.fetch_status === "failed" || m.fetch_status === "parse_error") ? (m.error_msg || "—") : <span style={{ color: "transparent" }}>·</span>}
                    </td>
                    <td>
                      <Num value={m.views} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.views} /></div>
                    </td>
                    <td>
                      <Num value={m.likes} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.likes} /></div>
                    </td>
                    <td>
                      <Num value={m.collects} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.collects} /></div>
                    </td>
                    <td>
                      <Num value={m.comments} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.comments} /></div>
                    </td>
                    <td>
                      <Num value={m.interaction_rate} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.interaction_rate} /></div>
                    </td>
                    <td>
                      <Num value={m.like_rate} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.like_rate} /></div>
                    </td>
                    <td>
                      <Num value={m.collect_rate} />
                      <div style={{ fontSize: 10 }}><Z value={m.z?.collect_rate} /></div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {metrics && (
          <div className="note" style={{ marginTop: 10 }}>
            每格上方为原始值，下方为相对本平台平均水平的偏离（正数高于平均）。同一平台至少 2 篇有效样本才会计算，否则显示「—」。
            {zStats.length === 0 && " 当前各平台样本都不足，暂时无法比较。"}
          </div>
        )}

        {metrics && zStats.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <details>
            <summary className="form-label" style={{ cursor: "pointer" }}>平台基准（平均值 / 波动 / 样本数）</summary>
            <div className="table-wrap">
              <table>
                <thead><tr><th>平台</th><th>指标</th><th>平均值</th><th>波动</th><th>样本数</th><th>可跨平台对比</th></tr></thead>
                <tbody>
                  {zStats.map((s, i) => (
                    <tr key={i}>
                      <td>{PLATFORM_NAME[s.platform as Platform] ?? s.platform}</td>
                      <td>{METRIC_NAME[s.metric] ?? s.metric}</td>
                      <td>{s.mean}</td>
                      <td>{s.std}</td>
                      <td>{s.sample_count}</td>
                      <td>
                        {(metrics.cross_platform_comparable ?? []).includes(s.metric)
                          ? <span className="tag green">可对比</span>
                          : <span className="tag gray">仅平台内</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            </details>
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">△</span>复盘策略调整建议</div>
            <div className="panel-sub">根据最近的发布数据自动生成</div>
          </div>
        </div>
        {gapsError && (
          <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
            <span>! {gapsError}</span><button className="btn sm" onClick={loadGaps}>重试</button>
          </div>
        )}
        {!analysis && !gapsError ? <div className="loading"><div className="spinner" />加载中...</div> : analysis && (
          <>
            {adjustments.length === 0 ? (
              <EmptyHint next="跑满一轮并完成数据采集后，再回到这里查看调整建议">暂无策略调整建议</EmptyHint>
            ) : (
              <div className="list">
                {adjustments.map((s, i) => (
                  <div key={i} className="task-row">
                    <i className="task-prio blue" />
                    <div className="task-main">
                      <div className="task-title">{s.type}</div>
                      <div className="task-desc">{s.description}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {analysis.evidence && Object.keys(analysis.evidence).length > 0 && (
              <details style={{ marginTop: 18 }}>
                <summary className="form-label" style={{ cursor: "pointer" }}>查看触发数据</summary>
                <pre style={{ background: "var(--surface-2)", padding: 12, borderRadius: 8, fontSize: 11, overflow: "auto" }}>
{JSON.stringify(analysis.evidence, null, 2)}
                </pre>
              </details>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◫</span>缺口分析</div>
            <div className="panel-sub">覆盖缺口可直接采纳进母计划；验证缺口仅 Agent 内部参考</div>
          </div>
        </div>
        {adoptMsg && (
          <div className="panel-sub" style={{ padding: "0 16px 8px", color: "var(--muted)" }}>{adoptMsg}</div>
        )}
        {!analysis ? (
          <EmptyHint next="完成一轮复盘后这里会列出覆盖缺口与验证缺口">暂无缺口数据</EmptyHint>
        ) : (() => {
          const coverage = analysis.gaps.filter((g) => g.audience === "user");
          const validation = analysis.gaps.filter((g) => g.audience !== "user");
          return (
            <>
              {coverage.length === 0 ? (
                <EmptyHint next="当前各维度覆盖均衡，跑满一轮后重新评估">暂无覆盖缺口</EmptyHint>
              ) : (
                <div className="list">
                  {coverage.map((g, i) => (
                    <div key={`c-${i}`} className="task-row">
                      <i className="task-prio blue" />
                      <div className="task-main">
                        <div className="task-title">{g.dimension}</div>
                        <div className="task-desc">{g.suggestion_text}</div>
                        <div className="task-desc" style={{ color: "var(--subtle)", fontSize: 11 }}>
                          现有 <Num value={g.current_count} /> 条 · 建议补至 <Num value={g.target_count} /> 条 ·
                          样本 <Num value={g.sample_count} /> · 平均互动率 <Num value={g.avg_interaction_rate} />
                        </div>
                      </div>
                      <button
                        className="btn sm primary"
                        disabled={adopting === g.dimension}
                        onClick={() => adoptGap(g.dimension)}
                      >
                        {adopting === g.dimension ? "采纳中…" : "✓ 采纳到母计划"}
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {validation.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <div className="form-label" style={{ color: "var(--subtle)" }}>
                    Agent 内部建议（无需用户确认）
                  </div>
                  {validation.map((g, i) => (
                    <div key={`v-${i}`} style={{ color: "var(--subtle)", fontSize: 12, marginTop: 6 }}>
                      · {g.dimension}：{g.suggestion_text}（有效样本 {g.sample_count} 条，待下一轮受控试验）
                    </div>
                  ))}
                </div>
              )}
            </>
          );
        })()}
      </div>
      </>}
    </>
  );
}
