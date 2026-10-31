import { useCallback, useEffect, useMemo, useState } from "react";
import { api, GlobalMonitorResponse, GlobalTaskQueueItem } from "../api";
import { navigate } from "../nav";

const STATUS: Record<string, string> = {
  queued: "排队中", running: "运行中", succeeded: "已完成",
  failed: "失败", cancelled: "已取消", interrupted: "中断",
};
const PLATFORMS: Record<string, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
};

export default function GlobalOperations() {
  const [monitor, setMonitor] = useState<GlobalMonitorResponse | null>(null);
  const [tasks, setTasks] = useState<GlobalTaskQueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([api.globalMonitor(30), api.taskQueue()]);
    if (results[0].status === "fulfilled") setMonitor(results[0].value);
    if (results[1].status === "fulfilled") setTasks(results[1].value.items ?? []);
    const failures = results.flatMap((r, i) => r.status === "rejected"
      ? [`${i === 0 ? "账号组汇总" : "全局任务队列"}：${r.reason instanceof Error ? r.reason.message : String(r.reason)}`]
      : []);
    setError(failures.join("；"));
    setUpdatedAt(new Date());
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const active = useMemo(() => tasks.filter((t) => t.status === "queued" || t.status === "running"), [tasks]);
  const failed = useMemo(() => tasks.filter((t) => t.status === "failed" || t.status === "interrupted"), [tasks]);
  const byTenant = useMemo(() => {
    const map = new Map<string, { queued: number; running: number; failed: number }>();
    for (const task of tasks) {
      const value = map.get(task.tenant_id) ?? { queued: 0, running: 0, failed: 0 };
      if (task.status === "queued") value.queued++;
      if (task.status === "running") value.running++;
      if (task.status === "failed" || task.status === "interrupted") value.failed++;
      map.set(task.tenant_id, value);
    }
    return map;
  }, [tasks]);

  return (
    <div className="page global-operations-page">
      <div className="page-head">
        <div>
          <div className="eyebrow">GLOBAL OPERATIONS</div>
          <h1 className="page-title">全局运营中心</h1>
          <p className="page-desc">跨账号组独立总览，不受当前账号组切换影响。任务状态来自全局队列，内容统计来自各账号组数据库。</p>
          {updatedAt && <div className="tiny">更新于 {updatedAt.toLocaleTimeString("zh-CN")}</div>}
        </div>
        <div className="head-actions">
          <button className="btn sm" onClick={() => void refresh()} disabled={loading}>{loading ? "刷新中…" : "↻ 刷新"}</button>
          <button className="btn sm" onClick={() => navigate("task-queue")}>查看任务队列</button>
        </div>
      </div>

      {error && <div className="panel" role="alert" style={{ borderColor: "var(--orange)", color: "var(--orange)" }}>
        部分数据加载失败：{error}。未成功加载的指标不会按 0 处理。
      </div>}

      <div className="grid four" style={{ marginBottom: 16 }}>
        <div className="metric"><div className="metric-label">账号组</div><div className="metric-val">{monitor?.totals.tenants ?? "—"}</div><div className="metric-foot">数据库可读取 {monitor?.totals.available_tenants ?? "—"} 个</div></div>
        <div className="metric"><div className="metric-label">运行中任务</div><div className="metric-val">{tasks.length ? active.filter((t) => t.status === "running").length : monitor ? 0 : "—"}</div><div className="metric-foot">来自全局队列当前返回记录</div></div>
        <div className="metric"><div className="metric-label">等待任务</div><div className="metric-val">{tasks.length ? active.filter((t) => t.status === "queued").length : monitor ? 0 : "—"}</div><div className="metric-foot">按全局 FIFO 入队顺序</div></div>
        <div className="metric"><div className="metric-label">失败 / 中断</div><div className="metric-val">{tasks.length ? failed.length : monitor ? 0 : "—"}</div><div className="metric-foot">当前队列返回范围内</div></div>
      </div>

      <section className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div><div className="panel-title">账号组运行矩阵</div><div className="panel-sub">账号组数据库状态与任务分布；查看详情不会切换当前账号组。</div></div>
          <span className="muted">{monitor?.tenants.length ?? "—"} 个账号组</span>
        </div>
        {loading && !monitor ? <div className="empty-state">正在汇总账号组…</div> : !monitor ? <div className="empty-state">账号组数据暂不可用。</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>账号组</th><th>数据库</th><th>近 30 天内容</th><th>草稿</th><th>运行中</th><th>排队中</th><th>失败 / 中断</th><th>操作</th></tr></thead>
              <tbody>{monitor.tenants.map((tenant) => {
                const q = byTenant.get(tenant.tenant_id) ?? { queued: 0, running: 0, failed: 0 };
                return <tr key={tenant.tenant_id}>
                  <td><strong>{tenant.tenant_name}</strong><div className="tiny">{tenant.tenant_id}</div></td>
                  <td><span className={tenant.db_available ? "tag green" : "tag red"}>{tenant.db_available ? "可读取" : "不可用"}</span></td>
                  <td>{tenant.contents_period}</td><td>{tenant.drafts_all}</td>
                  <td>{q.running}</td><td>{q.queued}</td><td>{q.failed}</td>
                  <td><button className="btn sm" onClick={() => navigate("global-monitor")}>统计详情</button></td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid two">
        <section className="panel">
          <div className="panel-head"><div><div className="panel-title">全局 FIFO 任务</div><div className="panel-sub">运行任务不会因账号组切换而中断。</div></div><button className="btn sm" onClick={() => navigate("task-queue")}>完整队列</button></div>
          {active.length ? <div className="list">{active.slice(0, 12).map((task) => <div className="task-row" key={task.task_id}>
            <i className={`task-prio ${task.status === "running" ? "green" : "blue"}`} />
            <div className="task-main"><div className="task-title">{task.tenant_name} · {task.task_type === "round_run" ? "内容生产" : task.task_type}</div><div className="task-desc">#{task.queue_order} · {STATUS[task.status]} · {task.aggregate_id ?? "—"}</div></div>
          </div>)}</div> : <div className="empty-state">当前没有排队或执行中的任务。</div>}
          {active.length > 12 && <div className="panel-sub">另有 {active.length - 12} 个任务，请进入完整队列查看。</div>}
        </section>
        <section className="panel">
          <div className="panel-head"><div><div className="panel-title">异常与待处理</div><div className="panel-sub">当前返回的队列记录中失败或中断的任务。</div></div><span className="tag">{failed.length} 项</span></div>
          {failed.length ? <div className="list">{failed.slice(0, 12).map((task) => <div className="task-row" key={task.task_id}>
            <i className="task-prio red" /><div className="task-main"><div className="task-title">{task.tenant_name} · {STATUS[task.status]}</div><div className="task-desc">{task.error_message || task.task_type}</div></div>
          </div>)}</div> : <div className="empty-state">当前返回范围内没有失败或中断任务。</div>}
        </section>
      </div>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><div><div className="panel-title">平台与内容汇总</div><div className="panel-sub">近 30 天跨账号组内容产出；平台账号登录状态将在全局状态 API 阶段接入。</div></div></div>
        <div className="grid four">
          {Object.entries(monitor?.platform_totals ?? {}).map(([platform, value]) => <div className="metric" key={platform}>
            <div className="metric-label">{PLATFORMS[platform] ?? platform}</div><div className="metric-val">{value.contents_period}</div><div className="metric-foot">近 30 天内容 · 草稿 {value.drafts}</div>
          </div>)}
        </div>
        {!monitor && <div className="empty-state">等待汇总数据。</div>}
      </section>
    </div>
  );
}
