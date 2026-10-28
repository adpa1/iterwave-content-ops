import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import { api, SpiralRound, Goal } from "../api";

export default function Dashboard() {
  const { status } = useApp();
  const [rounds, setRounds] = useState<SpiralRound[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [r, g] = await Promise.allSettled([api.rounds(), api.goals()]);
      if (r.status === "fulfilled") setRounds(r.value as SpiralRound[]);
      if (g.status === "fulfilled") setGoals(g.value as Goal[]);
      if (r.status === "rejected" || g.status === "rejected") {
        setError("部分数据加载失败");
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  return (
    <AsyncPanel loading={loading} error={error} onRetry={load} loadingText="加载工作台...">
      <div className="page-head">
        <div>
          <div className="eyebrow">CONTROL CENTER</div>
          <h1 className="page-title">工作台概览</h1>
          <div className="page-desc">
            四平台图文运营 Agent · 活跃轮次 {status?.active_rounds ?? 0} · 已发布样本 {status?.published_samples ?? 0}
          </div>
        </div>
      </div>

      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⟳</span>最近轮次</div>
              <div className="panel-sub">螺旋引擎 9 态状态机流转记录</div>
            </div>
          </div>
          {rounds.length === 0 ? (
            <EmptyHint next="到「内容生产」创建第一轮">暂无轮次</EmptyHint>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>轮次</th><th>状态</th><th>创建时间</th></tr>
                </thead>
                <tbody>
                  {rounds.slice(0, 6).map((r) => (
                    <tr key={r.id}>
                      <td>Round {r.round_number}</td>
                      <td><span className={`tag ${statusTag(r.status)}`}>{r.status}</span></td>
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

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">◎</span>活跃目标</div>
              <div className="panel-sub">阶段目标（R21 口径：未知 ≠ 0）</div>
            </div>
          </div>
          {goals.length === 0 ? (
            <EmptyHint next="到「目标与策略」创建第一个阶段目标">暂无活跃目标</EmptyHint>
          ) : (
            <div className="list">
              {goals.filter((g) => g.status === "active").map((g) => (
                <div key={g.id} className="task-row">
                  <i className="task-prio blue" />
                  <div className="task-main">
                    <div className="task-title">{g.goal_text}</div>
                    <div className="task-desc">
                      {(g.target_platform as string) || "全平台"} · {g.target_metric} {g.target_value}
                    </div>
                  </div>
                  {g.achievement_rate != null ? (
                    <div style={{ fontSize: 12, fontWeight: 600 }}>{g.achievement_rate.toFixed(0)}%</div>
                  ) : (
                    <span className="tag gray">待采集</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </AsyncPanel>
  );
}

function statusTag(status: string): string {
  const cls: Record<string, string> = {
    created: "gray", producing: "orange", awaiting_publish: "blue",
    collecting_metrics: "purple", analyzing: "teal",
    completed: "green", partial_completed: "orange", failed: "red", cancelled: "gray",
  };
  return cls[status] ?? "gray";
}
