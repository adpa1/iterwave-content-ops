import { useEffect, useState } from "react";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import { api, Platform, PlatformInsight, SpiralAnalysis } from "../api";

const PLATFORM_LABEL: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音",
  kuaishou: "快手", wechat_channels: "视频号",
};

/** R3：NULL 显示灰色「—」，不显示 0 */
function Pct({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined) return <span style={{ color: "var(--subtle)" }}>—</span>;
  return <>{(v * 100).toFixed(2)}%</>;
}

export default function Gaps() {
  const [analysis, setAnalysis] = useState<SpiralAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pf, setPf] = useState<Platform | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setAnalysis(await api.gaps());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const breakdown: PlatformInsight[] = analysis?.platform_breakdown ?? [];
  // 手动选过平台就尊重选择；否则默认落在第一个有样本的平台上（避免首屏全是「—」）
  const active: PlatformInsight | undefined =
    breakdown.find((b) => b.platform === pf)
    ?? breakdown.find((b) => b.sample_count > 0)
    ?? breakdown[0];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">GAP ANALYSIS</div>
          <h1 className="page-title">缺口分析</h1>
          <div className="page-desc">
            两类缺口分开展示：<strong>覆盖缺口</strong>（绿色，需要你决定是否补内容）和 <strong>验证缺口</strong>（紫色，Agent 内部安排的小样本试验）。
          </div>
        </div>
      </div>

      <AsyncPanel
        loading={loading}
        error={error}
        onRetry={load}
        empty={!analysis}
        emptyText={
          <EmptyHint className="" next="先运行一轮并完成数据采集，再回到本页查看缺口">暂无缺口数据</EmptyHint>
        }
        boxed={false}
      >
        {analysis && (
        <>
          <div className="grid two" style={{ marginBottom: 18 }}>
            <div className="metric">
              <div className="metric-label">用户可见缺口</div>
              <div className="metric-val" style={{ color: "var(--green)" }}>
                {analysis.gaps.filter((g) => g.audience === "user").length}
              </div>
              <div className="metric-foot">建议补充选题</div>
            </div>
            <div className="metric">
              <div className="metric-label">Agent 内部缺口</div>
              <div className="metric-val" style={{ color: "var(--purple)" }}>
                {analysis.gaps.filter((g) => g.audience === "agent").length}
              </div>
              <div className="metric-foot">Agent 小样本试水</div>
            </div>
          </div>

          {analysis.gaps.length === 0 ? (
            <div className="panel">
              <EmptyHint next="保持当前选题配比，下一轮数据采集后再复查">暂无缺口建议</EmptyHint>
            </div>
          ) : (
            <div className="grid two">
              {analysis.gaps.map((g, i) => {
                const isUser = g.audience === "user";
                return (
                  <div key={i} className={`gap-card ${isUser ? "user" : "agent"}`}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                      <span className={`tag ${isUser ? "green" : "purple"}`}>
                        {isUser ? "◉ 给用户" : "✳ Agent 内部"}
                      </span>
                      <span className="tag gray">{g.gap_type}</span>
                      <span style={{ fontSize: 12, fontWeight: 700 }}>{g.dimension}</span>
                    </div>
                    <div style={{ fontSize: 13, marginBottom: 10 }}>{g.suggestion_text}</div>
                    <div style={{ display: "flex", gap: 14, fontSize: 11, color: "var(--muted)" }}>
                      <span>库存 {g.current_count}</span>
                      <span>样本 {g.sample_count}</span>
                      {g.avg_interaction_rate != null && (
                        <span>互动率 {(g.avg_interaction_rate * 100).toFixed(2)}%</span>
                      )}
                      <span>置信度 {g.confidence}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* G-04 单平台纵深：同口径拆到单平台内部 */}
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">⌁</span>单平台纵深</div>
                <div className="panel-sub">
                  同口径拆到单平台内部看题材 / 时段 / 标题长度 / 卡片风格与互动率的关系（窗口 {active?.days_analyzed ?? 90} 天，样本按平台内部统计）
                </div>
              </div>
              <div className="flex wrap" style={{ gap: 6 }}>
                {breakdown.map((b) => (
                  <button
                    key={b.platform}
                    className={`btn sm ${(active?.platform === b.platform) ? "primary" : ""}`}
                    onClick={() => setPf(b.platform)}
                  >
                    {PLATFORM_LABEL[b.platform]}
                    {b.sample_count === 0 && <span className="tag gray" style={{ marginLeft: 6 }}>无样本</span>}
                  </button>
                ))}
              </div>
            </div>

            {!active ? (
              <EmptyHint next="到「内容生产」运行一轮，用户发布并采集成功后再看单平台纵深">暂无纵深数据</EmptyHint>
            ) : (
              <>
                <div className="note" style={{ marginBottom: 12 }}>{active.conclusion}</div>

                <div className="grid four" style={{ marginBottom: 14 }}>
                  <div className="metric">
                    <div className="metric-label">样本数</div>
                    <div className="metric-val">{active.sample_count}</div>
                    <div className="metric-foot">success + partial</div>
                  </div>
                  <div className="metric">
                    <div className="metric-label">平均互动率</div>
                    <div className="metric-val"><Pct v={active.avg_interaction_rate} /></div>
                    <div className="metric-foot">平台内部均值</div>
                  </div>
                  <div className="metric">
                    <div className="metric-label">P50 中位数</div>
                    <div className="metric-val"><Pct v={active.p50_interaction_rate} /></div>
                    <div className="metric-foot">高于此值 = 优于本平台一半内容</div>
                  </div>
                  <div className="metric">
                    <div className="metric-label">P25 / P75</div>
                    <div className="metric-val" style={{ fontSize: 16 }}>
                      <Pct v={active.p25_interaction_rate} /> — <Pct v={active.p75_interaction_rate} />
                    </div>
                    <div className="metric-foot">四分位区间</div>
                  </div>
                </div>

                <div className="grid two">
                  <div>
                    <div className="panel-sub" style={{ marginBottom: 8 }}>题材表现（Top 5）</div>
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>维度</th><th>样本</th><th>平均互动率</th></tr></thead>
                        <tbody>
                          {active.top_dimensions.length === 0 && (
                            <tr><td colSpan={3}>
                              <EmptyHint className="" next="该平台样本 ≥2 后按题材聚合，先去「内容生产」跑一轮">无题材数据</EmptyHint>
                            </td></tr>
                          )}
                          {active.top_dimensions.map((d) => (
                            <tr key={d.dimension}>
                              <td>{d.dimension}</td>
                              <td>{d.sample_count}</td>
                              <td><Pct v={d.avg_interaction_rate} /></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  <div>
                    <div className="panel-sub" style={{ marginBottom: 8 }}>发布时段 / 标题长度</div>
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>类型</th><th>取值</th><th>样本</th><th>平均互动率</th></tr></thead>
                        <tbody>
                          {active.best_hours.length === 0 && active.best_title_style.best_bucket === undefined && (
                            <tr><td colSpan={4}>
                              <EmptyHint className="" next="到「内容日历」为草稿设置排期，用户确认发布后这里会统计最佳时段">无发布时段数据</EmptyHint>
                            </td></tr>
                          )}
                          {active.best_hours.map((h) => (
                            <tr key={`h${h.hour}`}>
                              <td>时段</td>
                              <td>{String(h.hour).padStart(2, "0")}:00</td>
                              <td>{h.sample_count}</td>
                              <td><Pct v={h.avg_interaction_rate} /></td>
                            </tr>
                          ))}
                          {active.best_title_style.best_bucket !== undefined && (
                            <tr>
                              <td>标题长度</td>
                              <td>{active.best_title_style.best_bucket} 字</td>
                              <td>—</td>
                              <td>最佳桶（平均 {active.best_title_style.avg_title_len ?? "—"} 字）</td>
                            </tr>
                          )}
                          {active.best_card_style && (
                            <tr>
                              <td>卡片风格</td>
                              <td>{active.best_card_style.card_style}</td>
                              <td>{active.best_card_style.sample_count}</td>
                              <td><Pct v={active.best_card_style.avg_interaction_rate} /></td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        </>
        )}
      </AsyncPanel>
    </>
  );
}
