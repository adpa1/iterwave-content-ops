import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import DemoBadge from "../components/DemoBadge";
import EmptyHint from "../components/EmptyHint";
import {
  api, Platform, CircuitBreakerEvent, Draft,
  OpsKpiResponse, OpsLogItem, TaskRunItem, NotificationItem,
  SchedulerJob, RhythmConfig,
} from "../api";

const PLATFORM_NAME: Record<Platform, string> = {
  xhs: "📕 小红书", douyin: "🎵 抖音",
  kuaishou: "⚡ 快手", wechat_channels: "💚 视频号",
};

const ACTION_LABEL: Record<string, string> = {
  passive_reply: "被动回复", active_comment: "主动评论", follow: "关注", browse: "浏览",
};

const STATUS_LABEL: Record<string, string> = {
  success: "成功", failed: "失败", skipped: "跳过", circuit_broken: "熔断拦截",
  pending: "待执行", running: "执行中", interrupted: "中断",
  pending_verify: "待核实",
};

const STATUS_CLS: Record<string, string> = {
  success: "good", failed: "bad", skipped: "warn", circuit_broken: "bad",
  pending: "", running: "info", interrupted: "warn", pending_verify: "purple",
};

const LEVEL_CLS: Record<string, string> = {
  info: "", warning: "orange", error: "red", critical: "red",
};

type OpsTab = "kpi" | "logs" | "publish" | "breaker" | "tasks" | "scheduler" | "rhythm" | "notifications";

const TABS: { key: OpsTab; label: string; icon: string }[] = [
  { key: "kpi", label: "运营总控", icon: "▦" },
  { key: "logs", label: "评论与运行日志", icon: "≋" },
  { key: "publish", label: "发布确认与轮询", icon: "◷" },
  { key: "breaker", label: "平台熔断器", icon: "🚨" },
  { key: "tasks", label: "任务追踪", icon: "⌁" },
  { key: "scheduler", label: "定时任务", icon: "⏰" },
  { key: "rhythm", label: "节奏调度", icon: "♪" },
  { key: "notifications", label: "通知中心", icon: "🔔" },
];

export default function Operations() {
  const { showToast, withPending } = useApp();
  const [tab, setTab] = useState<OpsTab>("kpi");

  // 各子页数据源
  const [kpi, setKpi] = useState<OpsKpiResponse | null>(null);
  const [logs, setLogs] = useState<OpsLogItem[]>([]);
  const [tasks, setTasks] = useState<TaskRunItem[]>([]);
  const [notifs, setNotifs] = useState<NotificationItem[]>([]);
  const [jobs, setJobs] = useState<{ jobs: SchedulerJob[]; running: boolean } | null>(null);
  const [rhythm, setRhythm] = useState<RhythmConfig | null>(null);
  const [cb, setCb] = useState<{ events: CircuitBreakerEvent[]; broken_platforms: Platform[] } | null>(null);
  const [draftSb, setDraftSb] = useState<string>("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** KPI 展示窗口（1=今日 / 7 / 30 / 90），与 R12 评分 90 天口径独立 */
  const [kpiDays, setKpiDays] = useState(1);
  /** R6 之外的全局人工软暂停 */
  const [paused, setPaused] = useState(false);
  const [notifBusy, setNotifBusy] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        tab === "kpi" || tab === "logs" ? api.opsKpi(kpiDays) : Promise.resolve(null),
        tab === "logs" ? api.opsLogs(undefined, undefined, undefined, 50) : Promise.resolve(null),
        tab === "tasks" ? api.taskRuns(undefined, undefined, 50) : Promise.resolve(null),
        tab === "notifications" ? api.notifications(undefined, 30) : Promise.resolve(null),
        tab === "scheduler" ? api.schedulerJobs() : Promise.resolve(null),
        tab === "rhythm" ? api.rhythm() : Promise.resolve(null),
        tab === "breaker" ? api.circuitBreaker() : Promise.resolve(null),
        tab === "kpi" ? api.opsPause() : Promise.resolve(null),
      ]);
      const unpack = (r: PromiseSettledResult<any>) => r.status === "fulfilled" ? r.value : null;
      setKpi(unpack(results[0]));
      const logsRes = unpack(results[1]);
      setLogs(logsRes?.items ?? []);
      setTasks(unpack(results[2]) ?? []);
      setNotifs(unpack(results[3]) ?? []);
      setJobs(unpack(results[4]));
      setRhythm(unpack(results[5]));
      setCb(unpack(results[6]));
      const pauseRes = unpack(results[7]);
      if (pauseRes) setPaused(Boolean(pauseRes.paused));
      // G-12：失败不能只进 console —— 收拢成可见错误 + 重试按钮
      const rejected = results.filter((r) => r.status === "rejected") as PromiseRejectedResult[];
      setLoadError(
        rejected.length > 0
          ? rejected.map((r) => r.reason instanceof Error ? r.reason.message : String(r.reason)).join("；")
          : null,
      );
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, [tab, kpiDays]);

  const togglePause = async () => {
    try {
      const r = await api.setOpsPause(!paused);
      setPaused(r.paused);
      showToast(r.paused ? "⏸️ 已暂停全部运营动作（存草稿/采集不受影响）" : "▶️ 已恢复运营", "success");
    } catch (e: any) { showToast(e.message, "error"); }
  };

  const testNotify = async () => {
    setNotifBusy(true);
    try {
      const r = await api.notifyTest();
      const label: Record<string, string> = { ok: "正常", failed: "失败", disabled: "未启用" };
      showToast(
        "通道自检：" + Object.entries(r.results).map(([k, v]) => `${k}=${label[v] ?? v}`).join(" · "),
        "info",
      );
    } catch (e: any) { showToast(e.message, "error"); }
    finally { setNotifBusy(false); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">OPERATIONS CONSOLE</div>
          <h1 className="page-title">运营控制台</h1>
          <div className="page-desc">
            9 个子面板覆盖运营全链路：KPI 总览、评论日志、发布确认、熔断器、任务追踪、定时任务、节奏调度、通知中心。
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {/* 全局软暂停：只停运营动作（评论/关注），存草稿与数据采集不受影响 */}
          <button
            className={`btn ${paused ? "primary" : ""}`}
            onClick={togglePause}
            title="仅暂停评论/关注等运营动作；存草稿与数据采集不受影响"
          >
            {paused ? "▶ 恢复运营" : "⏸ 暂停运营"}
          </button>
          <button className="btn" onClick={testNotify} disabled={notifBusy}>
            {notifBusy ? "自检中…" : "🔔 测试通知"}
          </button>
          <button className="btn" onClick={refresh} disabled={loading}>
            {loading ? "加载中…" : "↻ 刷新"}
          </button>
        </div>
      </div>

      {/* G-12：接口失败必须可见（不能只 console.error），并提供重试 */}
      {loadError && (
        <div className="panel" style={{ borderColor: "var(--red)", marginBottom: 14 }}>
          <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>⚠️ 接口异常：{loadError}</span>
            <button className="btn sm" onClick={refresh}>↻ 重试</button>
          </div>
        </div>
      )}

      {/* 子 tab bar */}
      <div className="compact-tabbar">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`compact-tab-button ${tab === t.key ? "active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            <span style={{ marginRight: 4 }}>{t.icon}</span>{t.label}
          </button>
        ))}
      </div>

      {/* === 1. 运营总控（KPI）=== */}
      {tab === "kpi" && <KpiPane kpi={kpi} days={kpiDays} onDaysChange={setKpiDays} />}

      {/* === 2. 评论日志 === */}
      {tab === "logs" && <LogsPane logs={logs} />}

      {/* === 3. 发布确认 === */}
      {tab === "publish" && <PublishPane />}

      {/* === 4. 熔断器 === */}
      {tab === "breaker" && (
        <BreakerPane cb={cb} onRecover={async (p) => {
          // G-11：同一平台的恢复/重置在请求中不可重复触发
          try {
            const r = await withPending(`cb:recover:${p}`, () => api.recoverCb(p));
            if (r) showToast(r.passed ? "✅ 恢复通过" : `❌ ${r.reason}`, r.passed ? "success" : "error");
          } catch (e: any) { showToast(e.message, "error"); }
          await refresh();
        }} onReset={async (p) => {
          try {
            await withPending(`cb:reset:${p}`, () => api.resetCb(p));
            showToast(`🔧 已重置 ${p}`);
          } catch (e: any) { showToast(e.message, "error"); }
          await refresh();
        }} />
      )}

      {/* === 5. 任务追踪 === */}
      {tab === "tasks" && <TasksPane tasks={tasks} onResolved={refresh} />}

      {/* === 6. 定时任务 === */}
      {tab === "scheduler" && <SchedulerPane jobs={jobs} onTriggered={refresh} />}

      {/* === 7. 节奏调度 === */}
      {tab === "rhythm" && <RhythmPane rhythm={rhythm} />}

      {/* === 8. 通知中心 === */}
      {tab === "notifications" && <NotificationsPane items={notifs} />}
    </>
  );
}

// =====================================================================
// 各子面板组件
// =====================================================================

function KpiPane({
  kpi, days, onDaysChange,
}: {
  kpi: OpsKpiResponse | null;
  days: number;
  onDaysChange: (d: number) => void;
}) {
  if (!kpi) return <div className="note">加载中…</div>;
  const platforms: Platform[] = ["xhs", "douyin", "kuaishou", "wechat_channels"];
  const draftTotal = Object.values(kpi.draft_status).reduce((a, b) => a + b, 0);
  const todayTotalOps = Object.values(kpi.today_ops).reduce(
    (acc, obj) => acc + Object.values(obj).reduce((a, b) => a + b, 0), 0
  );
  const rangeLabel = days === 1 ? "今日" : `近 ${days} 天`;

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <DemoBadge level="demo" text="平台运营动作（评论/关注）当前为适配器接入中，数字代表系统记录的尝试量，不代表平台侧真实结果" />
      </div>
      {/* KPI 时间窗口切换（与 R12 评分 90 天口径独立） */}
      <div className="compact-tabbar" style={{ marginBottom: 12 }}>
        {[1, 7, 30, 90].map((d) => (
          <button
            key={d}
            className={`compact-tab-button ${days === d ? "active" : ""}`}
            onClick={() => onDaysChange(d)}
          >
            {d === 1 ? "今日" : `${d} 天`}
          </button>
        ))}
      </div>
      {/* 顶部 KPI 卡 */}
      <div className="grid four" style={{ marginBottom: 16 }}>
        <KpiCard label={`${rangeLabel}运营动作`} value={todayTotalOps} hint={`ops_logs ${rangeLabel}执行量`} />
        <KpiCard label="草稿总数" value={draftTotal} hint="含各状态" />
        <KpiCard label="活跃熔断" value={kpi.circuit_breaker_active.length} hint="冷却中未恢复" color={kpi.circuit_breaker_active.length > 0 ? "var(--red)" : undefined} />
        <KpiCard label="启用平台" value={kpi.platform_stats ? Object.keys(kpi.platform_stats).length : 0} hint="" />
      </div>

      {/* 平台状态卡 */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">▦</span>平台运营状态</div>
            <div className="panel-sub">每平台活跃轮次 / 待发布草稿 / 已发布</div>
          </div>
        </div>
        <div className="grid four">
          {platforms.map((p) => {
            const s = kpi.platform_stats[p];
            if (!s) return null;
            return (
              <div key={p} className="platform-goal">
                <div className="platform-goal-head">
                  <div className="platform-name">{PLATFORM_NAME[p]}</div>
                  {s.circuit_broken ? <span className="tag red">🚨 熔断</span> : <span className="tag green">正常</span>}
                </div>
                <div className="goal-main">
                  <b>{s.rounds}</b><span>活跃轮次</span>
                </div>
                <div className="goal-foot">
                  <span>待发布 <strong>{s.drafts_saved}</strong></span>
                  <span>已发布 <strong>{s.drafts_published}</strong></span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 运营动作分布（随上方窗口切换） */}
      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div className="panel-title"><span className="icon">≋</span>{rangeLabel}运营动作分布</div>
          </div>
          <div className="list">
            {Object.keys(ACTION_LABEL).map((at) => {
              const obj = kpi.today_ops[at] || {};
              const total = Object.values(obj).reduce((a, b) => a + b, 0);
              return (
                <div key={at} className="task-row">
                  <i className={`task-prio ${obj.success > 0 ? "green" : obj.failed > 0 ? "red" : "blue"}`} />
                  <div className="task-main">
                    <div className="task-title">{ACTION_LABEL[at]}</div>
                    <div className="task-desc">
                      成功 {obj.success || 0} · 失败 {obj.failed || 0} · 跳过 {obj.skipped || 0}
                    </div>
                  </div>
                  <div style={{ fontSize: 20, fontWeight: 800 }}>{total}</div>
                </div>
              );
            })}
            {Object.keys(ACTION_LABEL).length === 0 && (
              <EmptyHint next="到「节奏调度」确认运营时段与每日配额">今日无运营动作</EmptyHint>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="panel-title"><span className="icon">⌁</span>草稿状态分布</div>
          </div>
          <div className="list">
            {Object.entries(kpi.draft_status).map(([status, n]) => (
              <div key={status} className="task-row">
                <i className={`task-prio ${status === "published" ? "green" : status === "failed" || status === "expired" ? "red" : "blue"}`} />
                <div className="task-main">
                  <div className="task-title">{status}</div>
                </div>
                <div style={{ fontSize: 18, fontWeight: 700 }}>{n}</div>
              </div>
            ))}
            {Object.keys(kpi.draft_status).length === 0 && (
              <EmptyHint next="到「内容生产 → 生产线」运行一轮并存草稿">暂无草稿</EmptyHint>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function KpiCard({ label, value, hint, color }: { label: string; value: number; hint?: string; color?: string }) {
  return (
    <div className="metric">
      <div className="metric-top">
        <div className="metric-label">{label}</div>
      </div>
      <div className="metric-val" style={color ? { color } : undefined}>{value}</div>
      {hint && <div className="metric-foot">{hint}</div>}
    </div>
  );
}

function LogsPane({ logs }: { logs: OpsLogItem[] }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">≋</span>运营日志（ops_logs）</div>
          <div className="panel-sub">最近 {logs.length} 条 — 被动回复 / 主动评论 / 关注</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>时间</th><th>平台</th><th>动作</th><th>内容摘要</th><th>状态</th></tr>
          </thead>
          <tbody>
            {logs.length === 0 && (
              <tr><td colSpan={5}>
                <EmptyHint className="" next="到「发布确认与轮询」确认已发布，或等 30min 评论轮询产生日志">暂无运营日志</EmptyHint>
              </td></tr>
            )}
            {logs.map((l) => (
              <tr key={l.id}>
                <td style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>
                  {l.executed_at ? new Date(l.executed_at).toLocaleString("zh-CN") : l.schedule_time ? "待执行" : "-"}
                </td>
                <td>{PLATFORM_NAME[l.platform]}</td>
                <td><span className="tag gray">{ACTION_LABEL[l.action_type] ?? l.action_type}</span></td>
                <td style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12 }}>
                  {l.content ?? l.target_author ?? "-"}
                </td>
                <td><span className={`demo-status ${STATUS_CLS[l.status] ?? ""}`}>{STATUS_LABEL[l.status] ?? l.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PublishPane() {
  const { showToast, withPending } = useApp();
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchDrafts = async () => {
    try {
      const all = await api.drafts();
      setDrafts(all.filter((d) => d.status === "draft_saved" || d.status === "user_published"));
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? "加载草稿失败");
    }
  };

  useEffect(() => { fetchDrafts(); }, []);

  const confirm = async (id: string) => {
    if (pending) return;
    setPending(id);
    try {
      // G-11：同一草稿的确认按钮在请求中禁用，重复点击被 withPending 拦截
      const res = await withPending(`publish:${id}`, () => api.confirmPublish(id));
      if (res) {
        // 行内即时更新，无需等整表刷新
        setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, status: res.status } : d)));
        showToast("✅ 已标记 user_published", "success");
        await fetchDrafts();
      }
    } catch (e: any) {
      setError(e?.message ?? "确认发布失败");
      showToast(`确认失败: ${e?.message ?? "未知错误"}`, "error");
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">◷</span>发布确认双通道</div>
          <div className="panel-sub">手动点「✅ 我已发布」或等 30min 只读轮询自动发现（R1：永不点发布）</div>
          <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
            <DemoBadge level="accelerated" text="真实轮询周期 30min（原型曾用 2s 演示）" />
            <DemoBadge level="blocked" text="R1 红线：系统永不点击发布按钮，只能由你手动发布" />
          </div>
        </div>
      </div>
      {error && (
        <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
          <span>⚠️ {error}</span>
          <button className="btn sm" onClick={fetchDrafts}>重试</button>
        </div>
      )}
      {drafts.length === 0 ? (
        <EmptyHint next="到「内容生产 → 生产线」运行一轮并勾选「执行存草稿」，返回这里确认发布">暂无待确认发布的草稿</EmptyHint>
      ) : (
        <div className="list">
          {drafts.map((d) => (
            <div key={d.id} className="task-row">
              <i className={`task-prio ${d.status === "user_published" ? "green" : "orange"}`} />
              <div className="task-main">
                <div className="task-title">{PLATFORM_NAME[d.platform as Platform]} · {d.title ?? d.id}</div>
                <div className="task-desc">
                  状态：<strong>{d.status}</strong> · {d.saved_at ? `存草稿 ${new Date(d.saved_at).toLocaleString("zh-CN")}` : ""}
                </div>
              </div>
              <div className="task-actions">
                {d.status === "draft_saved" && (
                  <button
                    className="btn sm success"
                    disabled={pending !== null}
                    onClick={() => confirm(d.id)}
                  >
                    {pending === d.id ? "提交中…" : "✅ 我已发布"}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function BreakerPane({
  cb, onRecover, onReset,
}: {
  cb: { events: CircuitBreakerEvent[]; broken_platforms: Platform[] } | null;
  onRecover: (p: Platform) => Promise<void>;
  onReset: (p: Platform) => Promise<void>;
}) {
  const platforms: Platform[] = ["xhs", "douyin", "kuaishou", "wechat_channels"];
  return (
    <>
      <div className="grid four" style={{ marginBottom: 16 }}>
        {platforms.map((p) => {
          const broken = cb?.broken_platforms.includes(p) ?? false;
          const evt = cb?.events.find((e) => e.platform === p && e.resolved_at == null);
          return (
            <div key={p} className="panel" style={{ borderColor: broken ? "#ffd2d9" : undefined }}>
              <div className="platform-goal-head" style={{ marginBottom: 6 }}>
                <div className="platform-name">{PLATFORM_NAME[p]}</div>
                <span className={`tag ${broken ? "red" : "green"}`}>{broken ? "🚨 熔断中" : "✅ 正常"}</span>
              </div>
              {evt && (
                <div style={{ fontSize: 11, color: "var(--muted)", lineHeight: 1.7 }}>
                  触发：{evt.reason}<br />
                  检测：{new Date(evt.detected_at).toLocaleString("zh-CN")}<br />
                  {evt.cooldown_until && <>冷却到期：{new Date(evt.cooldown_until).toLocaleString("zh-CN")}</>}
                </div>
              )}
              <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
                <button className="btn sm" onClick={() => onRecover(p)}>3 步恢复</button>
                <button className="btn sm danger" onClick={() => onReset(p)}>手动重置</button>
              </div>
            </div>
          );
        })}
      </div>
      <div className="panel">
        <div className="panel-head">
          <div className="panel-title"><span className="icon">≋</span>熔断事件历史</div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>时间</th><th>平台</th><th>原因</th><th>状态</th></tr></thead>
            <tbody>
              {cb?.events.length === 0 && (
                <tr><td colSpan={4}>
                  <EmptyHint className="" next="触发熔断后事件会出现在这里；也可在上方「平台熔断器」查看各平台状态">暂无事件</EmptyHint>
                </td></tr>
              )}
              {cb?.events.map((e) => (
                <tr key={e.id}>
                  <td style={{ fontSize: 12, color: "var(--muted)" }}>{new Date(e.detected_at).toLocaleString("zh-CN")}</td>
                  <td>{e.platform}</td>
                  <td><span className="tag orange">{e.reason}</span></td>
                  <td>{e.resolved_at ? <span className="tag green">已恢复</span> : <span className="tag red">熔断中</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function TasksPane({ tasks, onResolved }: {
  tasks: TaskRunItem[];
  onResolved?: () => void;
}) {
  const TYPE_LABEL: Record<string, string> = {
    publish_polling: "发布轮询", data_fetch: "数据采集", daily_ops: "每日运营",
    comment_polling: "评论轮询", cb_recovery: "熔断恢复", kb_scan: "题库扫描",
    draft_save: "存草稿", round_pipeline: "轮次主流程", data_retention_cleanup: "数据清理",
  };
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  /** R17：崩溃前可能已发出的动作，必须由用户人工核实，不能自动重试 */
  const resolve = async (id: number, action: "sent" | "not_sent") => {
    setBusyId(id);
    setErr(null);
    try {
      await api.resolveTaskRun(id, action);
      onResolved?.();
    } catch (e: any) {
      setErr(`核实失败: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">⌁</span>任务追踪（task_runs）</div>
          <div className="panel-sub">运行 / 中断 / 待核实 — 崩溃恢复的关键表</div>
        </div>
      </div>
      {err && <div className="panel-sub" style={{ padding: "0 16px 8px", color: "var(--red)" }}>{err}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>平台</th><th>状态</th><th>尝试</th><th>开始</th><th>结束</th><th>错误</th><th>操作</th></tr></thead>
          <tbody>
            {tasks.length === 0 && (
              <tr><td colSpan={9}>
                <EmptyHint className="" next="运行一轮生产，或等待定时任务触发后会自动记录">暂无任务记录</EmptyHint>
              </td></tr>
            )}
            {tasks.map((t) => (
              <tr key={t.id}>
                <td style={{ fontSize: 12, fontFamily: "monospace" }}>#{t.id}</td>
                <td>{TYPE_LABEL[t.task_type] ?? t.task_type}</td>
                <td>{t.platform ? PLATFORM_NAME[t.platform] : "-"}</td>
                <td><span className={`demo-status ${STATUS_CLS[t.status] ?? ""}`}>{STATUS_LABEL[t.status] ?? t.status}</span></td>
                <td>{t.attempt}/{t.max_attempts}</td>
                <td style={{ fontSize: 11, color: "var(--muted)" }}>{t.started_at ? new Date(t.started_at).toLocaleString("zh-CN") : "-"}</td>
                <td style={{ fontSize: 11, color: "var(--muted)" }}>{t.finished_at ? new Date(t.finished_at).toLocaleString("zh-CN") : "-"}</td>
                <td style={{ fontSize: 11, maxWidth: 200 }}>{t.error_msg ?? "-"}</td>
                <td>
                  {t.status === "pending_verify" ? (
                    <>
                      <button className="btn sm" disabled={busyId === t.id} onClick={() => resolve(t.id, "sent")}>✔ 已发出</button>
                      <button className="btn sm ghost" style={{ marginLeft: 6 }} disabled={busyId === t.id} onClick={() => resolve(t.id, "not_sent")}>✖ 未发出</button>
                    </>
                  ) : (
                    <span style={{ color: "var(--subtle)" }}>—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SchedulerPane({ jobs, onTriggered }: {
  jobs: { jobs: SchedulerJob[]; running: boolean } | null;
  onTriggered?: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (!jobs) return <div className="note">加载中…</div>;
  const APS_LABEL: Record<string, string> = {
    publish_polling: "发布轮询（30min）",
    data_fetch: "数据采集（动态 24h）",
    daily_ops: "每日运营（每天 8:00）",
    comment_polling: "评论轮询（30min）",
    cb_recovery: "熔断恢复（5min）",
    kb_scan: "题库扫描（每天 6:00）",
    data_retention_cleanup: "数据保留清理（每天 6:00）",
    round_timeout_sweeper: "轮次超时判定（每小时）",
    round_review: "轮次复盘（10min）",
  };
  const runNow = async (id: string) => {
    setBusyId(id);
    setMsg(null);
    try {
      await api.runSchedulerJob(id);
      setMsg(`已触发「${APS_LABEL[id] ?? id}」，可到「任务追踪」查看结果`);
      onTriggered?.();
    } catch (e: any) {
      setMsg(`触发失败: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  };
  return (
    <>
      <div className="grid two" style={{ marginBottom: 16 }}>
        <div className="metric">
          <div className="metric-label">APScheduler 状态</div>
          <div className="metric-val" style={{ color: jobs.running ? "var(--green)" : "var(--red)" }}>
            {jobs.running ? "✅ 运行中" : "❌ 已停止"}
          </div>
          <div className="metric-foot">{jobs.jobs.length} 个注册任务</div>
        </div>
      </div>
      <div className="panel">
        <div className="panel-head">
          <div className="panel-title"><span className="icon">⏰</span>定时任务（{jobs.jobs.length}）</div>
        </div>
        {msg && (
          <div className="panel-sub" style={{ padding: "0 16px 8px", color: "var(--subtle)" }}>{msg}</div>
        )}
        <div className="list">
          {jobs.jobs.length === 0 && (
            <EmptyHint next="检查后端 APScheduler 是否已启动（/api/scheduler）；启动后 7 类定时任务会自动注册">APScheduler 无注册任务</EmptyHint>
          )}
          {jobs.jobs.map((j) => (
            <div key={j.id} className="task-row">
              <i className="task-prio blue" />
              <div className="task-main">
                <div className="task-title">{APS_LABEL[j.id] ?? j.id}</div>
                <div className="task-desc">
                  Trigger: <code style={{ fontSize: 10 }}>{j.trigger}</code>
                  {j.next_run_time && <> · 下次: {new Date(j.next_run_time).toLocaleString("zh-CN")}</>}
                  {!j.next_run_time && <> · 未排定</>}
                </div>
              </div>
              <button
                className="btn sm"
                disabled={busyId === j.id}
                onClick={() => runNow(j.id)}
                title="立即触发一次（不影响后续排期）"
              >
                {busyId === j.id ? "触发中…" : "▶ 立即执行"}
              </button>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function RhythmPane({ rhythm }: { rhythm: RhythmConfig | null }) {
  if (!rhythm) return <div className="note">加载中…</div>;
  return (
    <div className="grid two">
      {rhythm.enabled_platforms.map((p) => (
        <div key={p} className="panel">
          <div className="panel-head">
            <div className="panel-title"><span className="icon">♪</span>{PLATFORM_NAME[p]} · 节奏配置</div>
          </div>
          <div className="list">
            <div className="task-row">
              <i className="task-prio blue" />
              <div className="task-main">
                <div className="task-title">每日配额</div>
                <div className="task-desc">
                  被动回复 {rhythm.quotas[p].passive_replies} 条 · 主动评论 {rhythm.quotas[p].active_comments} 条 · 关注 {rhythm.quotas[p].follows} 条
                </div>
              </div>
            </div>
            <div className="task-row">
              <i className="task-prio green" />
              <div className="task-main">
                <div className="task-title">发布时段窗口</div>
                <div className="task-desc">{rhythm.posting_windows[p]?.join(" / ") ?? "-"}</div>
              </div>
            </div>
          </div>
        </div>
      ))}
      <div className="panel" style={{ gridColumn: "1 / -1" }}>
        <div className="panel-head">
          <div className="panel-title"><span className="icon">≋</span>全局参数</div>
        </div>
        <div className="note">
          最小操作间隔 <strong>{rhythm.min_interval_seconds}</strong> 秒（防止风控） · 所有平台独立配额互不影响
        </div>
      </div>
    </div>
  );
}

function NotificationsPane({ items }: { items: NotificationItem[] }) {
  const TYPE_LABEL: Record<string, string> = {
    draft_saved: "草稿已存", draft_failed: "存草稿失败",
    circuit_broken: "熔断触发", login_expired: "登录过期",
    goal_completed: "目标达成", data_fetch_failed: "数据采集失败",
    round_completed: "轮次完成", circuit_recovered: "熔断恢复",
    publish_confirmed: "发布确认", quota_exhausted: "配额耗尽",
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="panel-title"><span className="icon">🔔</span>消息通知历史</div>
        <div className="panel-sub">7 场景 × 4 级 × macOS/企微/飞书</div>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>级别</th><th>类型</th><th>平台</th><th>标题</th><th>渠道</th></tr></thead>
          <tbody>
            {items.length === 0 && (
              <tr><td colSpan={6}>
                <EmptyHint className="" next="等待 30min 发布轮询或运行一轮生产，通知会记录在这里">暂无通知</EmptyHint>
              </td></tr>
            )}
            {items.map((n) => (
              <tr key={n.id}>
                <td style={{ fontSize: 11, color: "var(--muted)", whiteSpace: "nowrap" }}>{new Date(n.sent_at).toLocaleString("zh-CN")}</td>
                <td><span className={`tag ${LEVEL_CLS[n.level] ?? ""}`}>{n.level.toUpperCase()}</span></td>
                <td>{TYPE_LABEL[n.type] ?? n.type}</td>
                <td>{n.platform ? PLATFORM_NAME[n.platform] : "-"}</td>
                <td style={{ fontSize: 12, maxWidth: 280 }}>{n.title}</td>
                <td style={{ fontSize: 11 }}>{n.channel ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
