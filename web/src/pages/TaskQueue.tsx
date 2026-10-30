import { useCallback, useEffect, useMemo, useState } from "react";
import { api, GlobalTaskQueueItem } from "../api";
import { useApp } from "../AppContext";

const STATUS_LABEL: Record<GlobalTaskQueueItem["status"], string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  interrupted: "中断待处理",
};

const TASK_LABEL: Record<string, string> = {
  round_run: "内容生产 / 存草稿",
};

function timeLabel(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function TaskRow({
  task,
  onCancel,
  cancelling,
}: {
  task: GlobalTaskQueueItem;
  onCancel: (task: GlobalTaskQueueItem) => void;
  cancelling: boolean;
}) {
  const active = task.status === "queued" || task.status === "running";
  return (
    <div className="panel-row" style={{ display: "grid", gridTemplateColumns: "minmax(180px, 1.5fr) minmax(100px, .8fr) minmax(100px, .8fr) minmax(150px, 1fr) auto", gap: 14, alignItems: "center", padding: "14px 16px" }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 650 }}>{TASK_LABEL[task.task_type] || task.task_type}</div>
        <div className="muted" style={{ fontSize: 12, marginTop: 4, overflowWrap: "anywhere" }}>
          {task.tenant_name} · 业务对象 {task.aggregate_id || "—"}
        </div>
      </div>
      <div>
        <span className={task.status === "failed" || task.status === "interrupted" ? "status-pill danger" : task.status === "running" ? "status-pill active" : "status-pill"}>
          {STATUS_LABEL[task.status]}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{timeLabel(task.created_at)}</div>
      <div className="muted" style={{ fontSize: 12 }}>
        {task.error_message || (task.started_at ? `开始：${timeLabel(task.started_at)}` : "尚未开始")}
      </div>
      <div>
        {task.status === "queued" && (
          <button className="btn sm soft" type="button" disabled={cancelling} onClick={() => onCancel(task)}>
            {cancelling ? "取消中…" : "取消"}
          </button>
        )}
        {active && task.status === "running" && <span className="muted" style={{ fontSize: 12 }}>按队列顺序执行</span>}
      </div>
    </div>
  );
}

export default function TaskQueue() {
  const { showToast } = useApp();
  const [items, setItems] = useState<GlobalTaskQueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await api.taskQueue();
      setItems(response.items || []);
      setError("");
    } catch (err) {
      setError((err as Error).message || "任务队列读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const active = useMemo(
    () => items.filter((item) => item.status === "running" || item.status === "queued")
      .sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [items],
  );
  const history = useMemo(
    () => items.filter((item) => !["running", "queued"].includes(item.status))
      .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [items],
  );

  const cancel = async (task: GlobalTaskQueueItem) => {
    setCancellingId(task.task_id);
    try {
      await api.cancelTask(task.task_id);
      showToast("排队任务已取消", "success");
      await refresh();
    } catch (err) {
      showToast((err as Error).message || "取消失败", "error");
    } finally {
      setCancellingId(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <div className="eyebrow">GLOBAL EXECUTION</div>
          <h1>全局任务队列</h1>
          <p className="muted">跨账号组统一排队。切换当前账号组不会暂停、取消或改变已提交任务的执行顺序。</p>
        </div>
        <button className="btn sm soft" type="button" onClick={() => void refresh()}>立即刷新</button>
      </div>

      <div className="grid-3" style={{ marginBottom: 18 }}>
        <div className="panel stat-card"><div className="muted">排队与执行</div><div className="stat-value">{active.length}</div></div>
        <div className="panel stat-card"><div className="muted">正在执行</div><div className="stat-value">{active.filter((item) => item.status === "running").length}</div></div>
        <div className="panel stat-card"><div className="muted">等待执行</div><div className="stat-value">{active.filter((item) => item.status === "queued").length}</div></div>
      </div>

      {error && <div className="notice danger" role="alert">{error}</div>}
      <section className="panel" style={{ marginBottom: 20 }}>
        <div className="panel-head">
          <div><h2>待执行任务</h2><div className="muted">严格按提交时间先后执行；同一时刻只执行一个队列任务。</div></div>
          <span className="muted">{active.length} 项</span>
        </div>
        {loading ? <div className="empty-state">正在读取队列…</div> : active.length ? (
          <div>{active.map((task) => <TaskRow key={task.task_id} task={task} onCancel={cancel} cancelling={cancellingId === task.task_id} />)}</div>
        ) : <div className="empty-state">当前没有等待或执行中的任务。</div>}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div><h2>最近任务</h2><div className="muted">包含成功、失败、中断和取消记录。</div></div>
          <span className="muted">最近 {history.length} 项</span>
        </div>
        {history.length ? <div>{history.map((task) => <TaskRow key={task.task_id} task={task} onCancel={cancel} cancelling={false} />)}</div> : <div className="empty-state">暂无历史任务。</div>}
      </section>
    </div>
  );
}
