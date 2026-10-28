import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import { api, SpiralRound, Goal, HotTopicItem, InsightItem } from "../api";
import { platformMeta, Platform } from "../platforms";

type Kpi = { label: string; value: string | number; hint?: string; tone?: "green" | "red" | "blue" };

export default function Dashboard() {
  const { status } = useApp();
  const [rounds, setRounds] = useState<SpiralRound[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [hottopics, setHottopics] = useState<HotTopicItem[]>([]);
  const [insights, setInsights] = useState<InsightItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const results = await Promise.allSettled([
        api.rounds(),
        api.goals(),
        api.hotTopics().catch(() => ({ items: [] })),
        api.insights().catch(() => ({ items: [] })),
      ]);
      if (results[0].status === "fulfilled") setRounds(results[0].value as SpiralRound[]);
      if (results[1].status === "fulfilled") setGoals(results[1].value as Goal[]);
      if (results[2].status === "fulfilled") {
        const v = results[2].value as any;
        setHottopics(Array.isArray(v) ? v : (v?.items ?? []));
      }
      if (results[3].status === "fulfilled") {
        const v = results[3].value as any;
        setInsights(Array.isArray(v) ? v : (v?.items ?? []));
      }
      if (results.some(r => r.status === "rejected")) setError("部分数据加载失败");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  // ===== 派生数据 =====
  const activeGoals = goals.filter(g => g.status === "active" || !g.status);
  const totalGoalTarget = activeGoals.reduce((s, g) => s + (g.target_value ?? 0), 0);
  const totalGoalCurrent = activeGoals.reduce((s, g) => s + (g.current_value ?? 0), 0);
  const overallAchievement = totalGoalTarget > 0 ? (totalGoalCurrent / totalGoalTarget) * 100 : null;

  // 平台进度（用 rounds 按 enabled 平台聚合）
  const enabledPlatforms = (status?.enabled_platforms as Platform[]) ?? ["xhs", "douyin", "kuaishou", "wechat_channels"];

  // KPI：最近 7 天已发布、进行中、活跃熔断
  const publishedRecent = rounds.filter(r => r.status === "completed" || r.status === "partial_completed").length;
  const producingNow = rounds.filter(r => r.status === "producing" || r.status === "awaiting_publish").length;
  const totalHotTopics = hottopics.length;

  return (
    <AsyncPanel loading={loading} error={error} onRetry={load} loadingText="加载工作台...">
      <div className="page-head">
        <div>
          <div className="eyebrow">CONTROL CENTER · 运营总览</div>
          <h1 className="page-title">工作台概览</h1>
          <div className="page-desc">
            IterWave · 活跃轮次 {status?.active_rounds ?? 0} · 已发布样本 {status?.published_samples ?? 0}
          </div>
        </div>
        <div className="page-head-actions" style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={load}>↻ 刷新</button>
          <a className="btn primary" href="?page=hot">✳ 扫描热点</a>
          <a className="btn primary" href="?page=agent">⚑ 查看 Agent 运行</a>
        </div>
      </div>

      {/* ===== 目标 Hero + KPI 卡 ===== */}
      <div className="panel hero-panel" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div className="panel-title"><span className="icon">◎</span>总体目标与阶段进度</div>
            <div className="panel-sub">
              {activeGoals.length > 0
                ? `共 ${activeGoals.length} 项阶段目标 · 本阶段至 ${(activeGoals[0] as any).metric_period_end ?? "—"}`
                : "暂无活跃目标"}
            </div>
          </div>
          {overallAchievement != null && (
            <div style={{ fontSize: 28, fontWeight: 750, color: "var(--accent)" }}>
              {overallAchievement.toFixed(0)}%
            </div>
          )}
        </div>
        <div style={{ height: 8, background: "var(--border)", borderRadius: 4, marginTop: 8, overflow: "hidden" }}>
          {overallAchievement != null && (
            <div style={{ width: `${Math.min(100, overallAchievement)}%`, height: "100%", background: "linear-gradient(90deg, #1f6bff, #00b8d4)" }} />
          )}
        </div>

        {/* 三项数字 */}
        <div style={{ display: "flex", gap: 24, marginTop: 16, flexWrap: "wrap" }}>
          <HeroItem label="总体目标增量" current={totalGoalCurrent} target={totalGoalTarget} />
          <HeroItem label="进行中轮次" value={producingNow} hint="生产中 + 待发布" />
          <HeroItem label="已复盘轮次" value={publishedRecent} hint="已完成 + 部分完成" />
          <HeroItem label="可用热点" value={totalHotTopics} hint="来自热点雷达的选题素材" />
        </div>
      </div>

      {/* ===== 4 个 KPI 卡 ===== */}
      <div className="kpi-row" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 }}>
        <KpiCard label="活跃目标" value={activeGoals.length} hint={`${activeGoals.filter(g => g.achievement_rate != null).length} 项有进度数据`} tone="blue" />
        <KpiCard label="进行中轮次" value={producingNow} hint="生产中 / 待发布" />
        <KpiCard label="已复盘完成" value={publishedRecent} hint="已完成 + 部分完成" tone="green" />
        <KpiCard label="可用热点" value={totalHotTopics} hint="扫描后入库的选题素材" />
      </div>

      {/* ===== 2 列主面板：左·活跃目标  右·最近轮次 ===== */}
      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">◎</span>活跃目标</div>
              <div className="panel-sub">阶段目标（R21 口径：未知 ≠ 0）</div>
            </div>
            <a href="?page=goals" className="btn ghost">管理 →</a>
          </div>
          {activeGoals.length === 0 ? (
            <EmptyHint next="到「目标与策略」创建第一个阶段目标">暂无活跃目标</EmptyHint>
          ) : (
            <div className="list">
              {activeGoals.map((g) => {
                const plat = (g.target_platform as Platform) ?? null;
                const m = plat ? platformMeta(plat) : null;
                return (
                  <div key={g.id} className="task-row">
                    <i className={`task-prio ${m ? "" : "blue"}`}
                       style={{ background: m?.color }} />
                    <div className="task-main">
                      <div className="task-title">{g.goal_text}</div>
                      <div className="task-desc">
                        {plat ? `${m?.icon} ${m?.label}` : "全平台"} · {g.target_metric} {g.target_value}
                      </div>
                    </div>
                    <div style={{ minWidth: 60, textAlign: "right" }}>
                      {g.achievement_rate != null ? (
                        <div style={{ fontSize: 14, fontWeight: 600, color: g.achievement_rate >= 100 ? "#12b76a" : "inherit" }}>
                          {g.achievement_rate.toFixed(0)}%
                        </div>
                      ) : (
                        <span className="tag gray" style={{ fontSize: 11 }}>待采集</span>
                      )}
                      {g.current_value != null && (
                        <div style={{ fontSize: 10, color: "var(--muted)" }}>
                          当前 {g.current_value} / 目标 {g.target_value}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⟳</span>最近轮次</div>
              <div className="panel-sub">螺旋引擎 9 态状态机流转记录</div>
            </div>
            <a href="?page=production" className="btn ghost">全部 →</a>
          </div>
          {rounds.length === 0 ? (
            <EmptyHint next="到「内容生产」创建第一轮">暂无轮次</EmptyHint>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>轮次</th><th>状态</th><th>发布</th><th>创建时间</th></tr>
                </thead>
                <tbody>
                  {rounds.slice(0, 8).map((r) => (
                    <tr key={r.id}>
                      <td>Round {r.round_number}</td>
                      <td><span className={`tag ${statusTag(r.status)}`}>{statusLabel(r.status)}</span></td>
                      <td style={{ fontSize: 12, color: "var(--muted)" }}>
                        {(r as any).user_published_at?.slice(0, 10) ?? "—"}
                      </td>
                      <td style={{ color: "var(--muted)", fontSize: 12 }}>
                        {new Date(r.created_at).toLocaleString("zh-CN")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* ===== 2 列：热点机会 · Agent 编排状态 ===== */}
      <div className="grid two" style={{ marginTop: 16 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">🔥</span>热点机会</div>
              <div className="panel-sub">来自 60s.viki.moe 公益 API · R22 证据状态</div>
            </div>
            <a href="?page=hot" className="btn ghost">全部 →</a>
          </div>
          {hottopics.length === 0 ? (
            <EmptyHint next="到「热点雷达」扫描">暂无热点数据</EmptyHint>
          ) : (
            <div className="list">
              {hottopics.slice(0, 5).map((h, i) => {
                const ht = h as any;
                return (
                  <div key={i} className="task-row">
                    <i className="task-prio blue" />
                    <div className="task-main">
                      <div className="task-title" style={{ maxWidth: 420 }}>{ht.title ?? ht.name ?? JSON.stringify(h).slice(0, 40)}</div>
                      <div className="task-desc">
                        <span className="tag gray" style={{ fontSize: 10 }}>
                          {ht.source ?? "—"}
                        </span>
                        {ht.hotness != null && <span style={{ marginLeft: 8 }}>🔥 {ht.hotness}</span>}
                      </div>
                    </div>
                    <a href={`?page=hot`} className="btn ghost" style={{ fontSize: 11 }}>→ 转选题</a>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚑</span>Agent 编排状态</div>
              <div className="panel-sub">RoundExecutor 7 节点流水线 · 实时状态</div>
            </div>
            <a href="?page=agent" className="btn ghost">详情 →</a>
          </div>
          <AgentPipelineStatus rounds={rounds} enabledPlatforms={enabledPlatforms as Platform[]} />
        </div>
      </div>

      {/* ===== 复盘洞察 ===== */}
      {insights.length > 0 && (
        <div className="panel" style={{ marginTop: 16 }}>
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">▤</span>复盘洞察 · 待你决策</div>
              <div className="panel-sub">AI 自动生成的策略调整建议（需人工采纳到母计划）</div>
            </div>
            <a href="?page=analytics" className="btn ghost">全部 →</a>
          </div>
          <div className="list">
            {insights.slice(0, 4).map((it: any, i: number) => (
              <div key={i} className="task-row">
                <i className="task-prio orange" />
                <div className="task-main">
                  <div className="task-title">{it.type ?? "建议"}：{it.description ?? JSON.stringify(it).slice(0, 60)}</div>
                </div>
                <a href={`?page=analytics`} className="btn ghost" style={{ fontSize: 11 }}>采纳 →</a>
              </div>
            ))}
          </div>
        </div>
      )}
    </AsyncPanel>
  );
}

// ============================================
// 子组件
// ============================================

function KpiCard({ label, value, hint, tone }: { label: string; value: string | number; hint?: string; tone?: "green" | "red" | "blue" }) {
  const colorMap: Record<string, string> = {
    green: "#12b76a", red: "#f85149", blue: "#1f6bff",
  };
  return (
    <div className="kpi-card" style={{
      background: "var(--panel)", border: "1px solid var(--border)",
      borderRadius: 10, padding: "14px 16px",
    }}>
      <div style={{ fontSize: 11, color: "var(--muted)", letterSpacing: 0.5 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 750, color: tone ? colorMap[tone] : "var(--text)", marginTop: 4 }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function HeroItem({ label, current, target, value, hint }: { label: string; current?: number; target?: number; value?: number; hint?: string }) {
  const v = value ?? current;
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 650, marginTop: 2 }}>
        {current != null && target != null && target > 0 ? (
          <>
            <span style={{ color: "var(--accent)" }}>+{current}</span>
            <span style={{ color: "var(--muted)", fontSize: 13 }}> / 目标 {target}</span>
          </>
        ) : v != null ? (
          String(v)
        ) : "—"}
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--muted)" }}>{hint}</div>}
    </div>
  );
}

function AgentPipelineStatus({ rounds, enabledPlatforms }: { rounds: SpiralRound[]; enabledPlatforms: Platform[] }) {
  // 统计各阶段在跑的轮次
  const phaseCount: Record<string, number> = {};
  for (const r of rounds) {
    phaseCount[r.status] = (phaseCount[r.status] ?? 0) + 1;
  }
  // 熔断/异常平台
  const brokenPlatforms = (window as any).__broken_platforms ?? [];

  const pipeline = [
    { key: "discover", label: "发现选题", count: rounds.length },
    { key: "judge", label: "研判评分", count: phaseCount.producing ?? 0 },
    { key: "create", label: "策划创作", count: phaseCount.producing ?? 0 },
    { key: "publish", label: "发布存草稿", count: phaseCount.awaiting_publish ?? 0 },
    { key: "ops", label: "运营执行", count: 0 },
    { key: "collect", label: "采集复盘", count: (phaseCount.collecting_metrics ?? 0) + (phaseCount.analyzing ?? 0) },
  ];

  return (
    <div>
      {/* 6 节点流水线条 */}
      <div style={{
        display: "flex", gap: 4, marginBottom: 12,
      }}>
        {pipeline.map((p, i) => (
          <div key={p.key} style={{
            flex: 1, textAlign: "center", padding: "6px 2px",
            borderRadius: 6, fontSize: 10,
            background: p.count > 0 ? "rgba(31,107,255,0.12)" : "var(--panel-alt)",
            color: p.count > 0 ? "var(--accent)" : "var(--muted)",
            border: `1px solid ${p.count > 0 ? "rgba(31,107,255,0.3)" : "var(--border)"}`,
          }}>
            <div>{String(i + 1).padStart(2, "0")}</div>
            <div style={{ fontWeight: 600 }}>{p.label}</div>
            <div style={{ opacity: 0.7 }}>{p.count > 0 ? `${p.count} 轮` : "—"}</div>
          </div>
        ))}
      </div>

      {/* 平台状态 */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {enabledPlatforms.map((p) => {
          const m = platformMeta(p);
          const broken = brokenPlatforms.includes(p);
          return (
            <div key={p} style={{
              display: "flex", alignItems: "center", gap: 4,
              padding: "4px 8px", borderRadius: 6,
              background: broken ? "rgba(248,81,73,0.08)" : "var(--panel-alt)",
              border: `1px solid ${broken ? "rgba(248,81,73,0.3)" : "var(--border)"}`,
              fontSize: 11, fontWeight: 500,
              color: broken ? "#f85149" : "var(--text)",
            }}>
              <span>{m.icon}</span>
              <span>{m.label}</span>
              <span className="dot" style={{
                width: 6, height: 6, borderRadius: 3,
                background: broken ? "#f85149" : "#12b76a",
                display: "inline-block",
              }} />
              <span style={{ fontSize: 10, color: "var(--muted)" }}>
                {broken ? "熔断" : "正常"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function statusTag(s: string): string {
  const cls: Record<string, string> = {
    created: "gray", producing: "orange", awaiting_publish: "blue",
    collecting_metrics: "purple", analyzing: "teal",
    completed: "green", partial_completed: "orange", failed: "red", cancelled: "gray",
  };
  return cls[s] ?? "gray";
}
function statusLabel(s: string): string {
  const map: Record<string, string> = {
    created: "创建", producing: "生产中", awaiting_publish: "待发布",
    collecting_metrics: "采集中", analyzing: "复盘中",
    completed: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已取消",
  };
  return map[s] ?? s;
}
