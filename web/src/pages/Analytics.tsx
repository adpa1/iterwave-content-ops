import { useEffect, useState } from "react";
import EmptyHint from "../components/EmptyHint";
import { api, MetricsResponse, MetricRow, Platform, SpiralAnalysis } from "../api";

const PLATFORM_NAME: Record<Platform, string> = {
  xhs: "📕 小红书", douyin: "🎵 抖音",
  kuaishou: "⚡ 快手", wechat_channels: "💚 视频号",
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

export default function Analytics() {
  const [analysis, setAnalysis] = useState<SpiralAnalysis | null>(null);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [platformFilter, setPlatformFilter] = useState<Platform | "">("");
  const [gapsError, setGapsError] = useState<string | null>(null);
  const [adopting, setAdopting] = useState<string | null>(null);
  const [adoptMsg, setAdoptMsg] = useState<string | null>(null);

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

  const rows: MetricRow[] = metrics?.items ?? [];
  const zStats = metrics?.z_stats ?? [];
  const adjustments = analysis?.strategy_adjustments ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">DATA NORMALIZATION & Z-SCORE</div>
          <h1 className="page-title">数据分析</h1>
          <div className="page-desc">
            z-score 平台内部基准（R13）。不同平台 +1σ 只代表「高于本平台平均水平 1σ」，不代表相同业务表现。跨平台可对比：互动率、收藏率、涨粉效率。
          </div>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={() => loadMetrics(platformFilter)}>↻ 刷新数据</button>
        </div>
      </div>

      <div className="grid three" style={{ marginBottom: 18 }}>
        <div className="metric">
          <div className="metric-label">归一化指标</div>
          <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 5 }}>
            {["interaction_rate", "like_rate", "collect_rate", "follower_efficiency"].map((m) => (
              <span key={m} style={{
                fontFamily: "monospace", fontSize: 11,
                background: "var(--blue-weak)", color: "var(--blue)",
                padding: "2px 7px", borderRadius: 4,
              }}>{m}</span>
            ))}
          </div>
          <div className="metric-foot" style={{ marginTop: 8 }}>views=NULL/0 → 全比率 NULL，不做除法（R3）</div>
        </div>
        <div className="metric">
          <div className="metric-label">z-score 参考</div>
          <div style={{ marginTop: 10, fontWeight: 700, fontSize: 15 }}>平台内部 mean / std</div>
          <div className="metric-foot" style={{ marginTop: 6 }}>
            时效窗口 {metrics?.z_window_days ?? 90} 天<br/>样本 &lt; 2 或 std=0 → z 为「—」
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">时效窗口</div>
          <div style={{ marginTop: 10, fontWeight: 700, fontSize: 15 }}>近 90 天发布</div>
          <div className="metric-foot" style={{ marginTop: 6 }}>过期样本不参与评分</div>
        </div>
      </div>

      {/* G-08 逐篇数据与归一化 */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">σ</span>逐篇数据与归一化</div>
            <div className="panel-sub">
              原始值来自平台读回（fetch_status 五态）；z 列为平台内部基准。
              <b>原始曝光/点赞/收藏不可跨平台对比</b>，仅互动率/点赞率/收藏率可跨平台比较。
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
            <span>⚠️ 数据加载失败：{metricsError}</span>
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
                      }`}>{m.fetch_status}</span>
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
            每格上方为原始值，下方为平台内 z-score。z 需要该平台该指标 ≥2 个有效样本且 std&gt;0，否则显示「—」（不是 0）。
            {zStats.length === 0 && " 当前所有平台样本均不足，暂无法计算 z-score。"}
          </div>
        )}

        {metrics && zStats.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="form-label">平台内基准（mean / std / 样本数）</div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>平台</th><th>指标</th><th>mean</th><th>std</th><th>样本数</th><th>可跨平台对比</th></tr></thead>
                <tbody>
                  {zStats.map((s, i) => (
                    <tr key={i}>
                      <td>{PLATFORM_NAME[s.platform as Platform] ?? s.platform}</td>
                      <td style={{ fontFamily: "monospace", fontSize: 11 }}>{s.metric}</td>
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
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">△</span>复盘策略调整建议（螺旋引擎输出）</div>
            <div className="panel-sub">来源：api/gaps 端点（真实后端）</div>
          </div>
        </div>
        {gapsError && (
          <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
            <span>⚠️ {gapsError}</span><button className="btn sm" onClick={loadGaps}>重试</button>
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
              <div style={{ marginTop: 18 }}>
                <div className="form-label">证据（触发数据）</div>
                <pre style={{ background: "var(--surface-2)", padding: 12, borderRadius: 8, fontSize: 11, overflow: "auto" }}>
{JSON.stringify(analysis.evidence, null, 2)}
                </pre>
              </div>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">◫</span>缺口分析（R19 两类缺口分开展示）</div>
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
