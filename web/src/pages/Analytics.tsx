import { useEffect, useState } from "react";
import EmptyHint from "../components/EmptyHint";
import { api, MetricsResponse, MetricRow, Platform, SpiralAnalysis, Draft, WeeklyReport, ActivationMetrics } from "../api";

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
    note_url: string; views: string; likes: string; collects: string;
    comments: string; shares: string; new_followers: string;
  }>>({});
  const [weeklyReport, setWeeklyReport] = useState<WeeklyReport | null>(null);
  const [activationMetrics, setActivationMetrics] = useState<ActivationMetrics | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);

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
  const loadMetrics = (p: Platform | "") => {
    setMetricsError(null);
    api.metrics(p || undefined).then(setMetrics).catch((e) => setMetricsError(e.message));
  };

  useEffect(() => { loadGaps(); }, []);
  useEffect(() => { loadMetrics(platformFilter); }, [platformFilter]);
  useEffect(() => {
    Promise.all([
      api.drafts(),
      api.weeklyReport(),
      api.activationMetrics(),
    ]).then(([allDrafts, report, metrics]) => {
      const published = allDrafts.filter((d) => ["user_published", "data_fetching", "published"].includes(d.status));
      setPublishedDrafts(published);
      setWeeklyReport(report);
      setActivationMetrics(metrics);
      setBackfillValues(Object.fromEntries(published.map((d) => [d.id, {
        note_url: d.platform_note_url || "", views: "", likes: "", collects: "",
        comments: "", shares: "", new_followers: "",
      }])));
    }).catch((e) => setReportError(e?.message || "发布回填与周报数据加载失败"));
  }, []);

  const saveBackfill = async (draft: Draft) => {
    const value = backfillValues[draft.id];
    if (!value) return;
    setBackfillBusy(draft.id);
    setReportError(null);
    try {
      if (value.note_url.trim() !== (draft.platform_note_url || "")) {
        await api.updateDraftNoteUrl(draft.id, value.note_url.trim());
      }
      const numeric = ["views", "likes", "collects", "comments", "shares", "new_followers"] as const;
      const metrics: Record<string, number | null> = {};
      for (const key of numeric) {
        const raw = value[key].trim();
        if (!raw) metrics[key] = null;
        else {
          const n = Number(raw);
          if (!Number.isInteger(n) || n < 0) throw new Error(`${key} 必须填写非负整数`);
          metrics[key] = n;
        }
      }
      if (numeric.some((key) => value[key].trim())) {
        await api.submitManualMetrics(draft.id, metrics);
      }
      const refreshed = await api.drafts();
      setPublishedDrafts(refreshed.filter((d) => ["user_published", "data_fetching", "published"].includes(d.status)));
      setBackfillValues((prev) => ({ ...prev, [draft.id]: {
        ...prev[draft.id], views: "", likes: "", collects: "", comments: "", shares: "", new_followers: "",
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
            每个平台只和自己的历史比较；互动率、点赞率、收藏率可以跨平台对比，曝光、点赞等原始数字不能。
          </div>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={() => loadMetrics(platformFilter)}>↻ 刷新数据</button>
        </div>
      </div>

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
        <div className="metric"><div className="metric-label">首次价值时间 TTV</div><div className="metric-val">{activationMetrics?.ttv_seconds == null ? "—" : `${Math.round(activationMetrics.ttv_seconds)} 秒`}</div><div className="metric-foot">首次进入快速体验至首批样稿生成</div></div>
        <div className="metric"><div className="metric-label">7 日激活率</div><div className="metric-val">{activationMetrics?.seven_day_activation_rate == null ? "观察中" : `${(activationMetrics.seven_day_activation_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">复制或保存至少一篇合格样稿</div></div>
        <div className="metric"><div className="metric-label">首次发布率</div><div className="metric-val">{activationMetrics?.first_publish_rate == null ? "观察中" : `${(activationMetrics.first_publish_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">初始化后 7 日内确认发布</div></div>
        <div className="metric"><div className="metric-label">数据回流完成率</div><div className="metric-val">{activationMetrics?.data_return_rate == null ? "—" : `${(activationMetrics.data_return_rate * 100).toFixed(0)}%`}</div><div className="metric-foot">已确认发布内容中至少有一份指标快照</div></div>
      </div>

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
                <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8, marginTop: 8 }}>
                  {(["views", "likes", "collects", "comments", "shares", "new_followers"] as const).map((key) => <label key={key} className="form-label">{({views:"曝光/播放",likes:"点赞",collects:"收藏",comments:"评论",shares:"分享",new_followers:"新增粉丝"})[key]}<input className="field" inputMode="numeric" value={value[key]} placeholder="留空=未知" onChange={(e) => setBackfillValues((p) => ({ ...p, [draft.id]: { ...p[draft.id], [key]: e.target.value } }))} /></label>)}
                </div>
                <button className="btn sm primary" style={{ marginTop: 10 }} disabled={backfillBusy === draft.id} onClick={() => saveBackfill(draft)}>{backfillBusy === draft.id ? "保存中…" : "保存链接与指标"}</button>
              </div>;
            })}
          </div>
        )}
      </div>

      <div className="panel" style={{ marginBottom: 15 }}>
        <div className="panel-head"><div><div className="panel-title">本周轻量复盘</div><div className="panel-sub">同平台比较；展示数据来源与样本量，不把相关性解释成因果。</div></div></div>
        {!weeklyReport ? <div className="loading"><div className="spinner" />加载周报…</div> : <>
          <div style={{ padding: "0 16px 16px" }}>
            <div className="panel-sub">{weeklyReport.period.start} 至 {weeklyReport.period.end} · 有效样本 {weeklyReport.sample_size} 篇 · 来源：{weeklyReport.data_sources.join("、") || "暂无"}</div>
            <h3 style={{ marginTop: 14 }}>表现最好</h3>
            {weeklyReport.best_content.length ? weeklyReport.best_content.map((item) => <div className="task-row" key={item.content_id}><div className="task-main"><div className="task-title">{item.title}</div><div className="task-desc">{PLATFORM_NAME[item.platform]} · {item.metric}：{item.value} · {item.source}</div></div></div>) : <div className="panel-sub">暂无有效指标，暂不排名。</div>}
            <h3 style={{ marginTop: 14 }}>可能原因</h3>
            {weeklyReport.why_it_may_work.map((item, i) => <p key={i} className="panel-sub">{item.statement}（样本 {item.sample_size}）</p>)}
            <h3 style={{ marginTop: 14 }}>下一轮建议</h3>
            {weeklyReport.next_actions.length ? weeklyReport.next_actions.map((item, i) => <div key={i} className="task-row"><div className="task-main"><div className="task-title">{item.action}</div><div className="task-desc">依据：{item.basis} · 验证：{item.verification}</div></div></div>) : <div className="panel-sub">样本不足，先补充发布数据，再生成策略建议。</div>}
            <div className="panel-sub" style={{ marginTop: 10 }}>限制：{weeklyReport.limitations.join("；")}</div>
          </div>
        </>}
      </div>

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
    </>
  );
}
