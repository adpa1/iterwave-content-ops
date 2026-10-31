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
  scheduler_job: "账号组后台任务",
};

const SCHEDULER_JOB_LABEL: Record<string, string> = {
  _job_publish_polling: "发布状态轮询",
  _job_data_fetch: "平台数据采集",
  _job_daily_ops: "每日主动运营",
  _job_comment_polling: "评论轮询",
  _job_cb_recovery: "平台连接恢复",
  _job_kb_scan: "选题库扫描",
  _job_kb_sync: "选题库同步",
  _job_data_retention_cleanup: "数据清理",
  _job_round_timeout_sweeper: "轮次超时检查",
  _job_round_review: "轮次复盘",
  _job_autogrow_next_round: "自动闭环检查",
};

function aggregateLabel(task: GlobalTaskQueueItem) {
  if (!task.aggregate_id) return "—";
  if (task.task_type === "round_run") {
    const [roundId, operation] = task.aggregate_id.split(":");
    return operation === "save_pending_drafts"
      ? `第 ${roundId} 轮 · 恢复存草稿`
      : `第 ${roundId} 轮`;
  }
  if (task.task_type !== "scheduler_job") {
    return task.aggregate_id;
  }
  const [method, objectId, attempt] = task.aggregate_id.split(":");
  const label = SCHEDULER_JOB_LABEL[method] || "账号组后台任务";
  if (method === "_job_data_fetch" && objectId) {
    return `${label} · ${objectId}${attempt ? ` · 第 ${attempt} 次` : ""}`;
  }
  return label;
}

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
          {task.tenant_name} · {aggregateLabel(task)}
        </div>
      </div>
      <div>
        <span className={`status-pill ${task.status === "failed" || task.status === "interrupted" ? "failed" : task.status === "running" ? "producing" : task.status === "succeeded" ? "completed" : task.status === "cancelled" ? "cancelled" : "creating"}`}>
          {STATUS_LABEL[task.status]}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{timeLabel(task.created_at)}</div>
      <div className="muted" style={{ fontSize: 12 }}>
        {task.error_message || (
          task.status === "queued" && new Date(task.available_at).getTime() > Date.now()
            ? `计划执行：${timeLabel(task.available_at)}`
            : task.started_at ? `开始：${timeLabel(task.started_at)}` : "尚未开始"
        )}
        {task.finished_at && <div style={{ marginTop: 4 }}>结束：{timeLabel(task.finished_at)}</div>}
        {task.started_at && task.finished_at && <div>耗时：{Math.max(0, (new Date(task.finished_at).getTime() - new Date(task.started_at).getTime()) / 1000).toFixed(1)} 秒</div>}
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
  const [activeItems, setActiveItems] = useState<GlobalTaskQueueItem[]>([]);
  const [historyItems, setHistoryItems] = useState<GlobalTaskQueueItem[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPages, setHistoryPages] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyType, setHistoryType] = useState<"all" | "round_run" | "scheduler_job">("round_run");
  const historyPageSize = 20;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [activeResponse, historyResponse] = await Promise.all([
        api.taskQueue({ scope: "active" }),
        api.taskQueue({ scope: "history", page: historyPage, pageSize: historyPageSize, taskType: historyType === "all" ? undefined : historyType }),
      ]);
      setActiveItems(activeResponse.items || []);
      setHistoryItems(historyResponse.items || []);
      setHistoryTotal(historyResponse.total ?? 0);
      setHistoryPages(historyResponse.pages ?? 1);
      setError("");
    } catch (err) {
      setError((err as Error).message || "任务队列读取失败");
    } finally {
      setLoading(false);
    }
  }, [historyPage, historyType]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const active = useMemo(
    () => [...activeItems].sort((a, b) => {
      const aDelayed = a.status === "queued" && new Date(a.available_at).getTime() > Date.now();
      const bDelayed = b.status === "queued" && new Date(b.available_at).getTime() > Date.now();
      if (aDelayed !== bDelayed) return aDelayed ? 1 : -1;
      return a.queue_order - b.queue_order;
    }),
    [activeItems],
  );
  const history = historyItems;

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

      <div className="queue-summary-grid" style={{ marginBottom: 18 }}>
        <div className="panel stat-card"><div className="muted">排队与执行</div><div className="stat-value">{active.length}</div></div>
        <div className="panel stat-card"><div className="muted">正在执行</div><div className="stat-value">{active.filter((item) => item.status === "running").length}</div></div>
        <div className="panel stat-card"><div className="muted">等待执行</div><div className="stat-value">{active.filter((item) => item.status === "queued").length}</div></div>
      </div>

      {error && <div className="notice danger" role="alert">{error}</div>}
      <section className="panel" style={{ marginBottom: 20 }}>
        <div className="panel-head">
          <div><h2>待执行任务</h2><div className="muted">已到执行时间的任务按入队顺序执行；延迟任务到期后参与排序，同一时刻只执行一个队列任务。</div></div>
          <span className="muted">{active.length} 项</span>
        </div>
        {loading ? <div className="empty-state">正在读取队列…</div> : active.length ? (
          <div>{active.map((task) => <TaskRow key={task.task_id} task={task} onCancel={cancel} cancelling={cancellingId === task.task_id} />)}</div>
        ) : <div className="empty-state">当前没有等待或执行中的任务。</div>}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div><h2>最近任务</h2><div className="muted">仅展示用户发起的内容生产任务；系统后台调度任务不混入历史列表。</div></div>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <select aria-label="历史任务类型" value={historyType} onChange={(event) => { setHistoryType(event.target.value as "all" | "round_run" | "scheduler_job"); setHistoryPage(1); }}>
              <option value="round_run">内容生产</option><option value="scheduler_job">系统后台任务</option><option value="all">全部任务类型</option>
            </select>
            <span className="muted">共 {historyTotal} 项 · 第 {historyPage}/{historyPages} 页</span>
          </div>
        </div>
        {history.length ? <div>{history.map((task) => <TaskRow key={task.task_id} task={task} onCancel={cancel} cancelling={false} />)}</div> : <div className="empty-state">暂无历史任务。</div>}
        <div className="panel-foot" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, padding: "12px 16px" }}>
          <span className="muted">每页 {historyPageSize} 条，历史记录由服务端分页读取。</span>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button className="btn sm" disabled={historyPage <= 1 || loading} onClick={() => setHistoryPage((p) => Math.max(1, p - 1))}>上一页</button>
            <span className="muted">{historyPage} / {historyPages}</span>
            <button className="btn sm" disabled={historyPage >= historyPages || loading} onClick={() => setHistoryPage((p) => Math.min(historyPages, p + 1))}>下一页</button>
          </div>
        </div>
      </section>
    </div>
  );
}
