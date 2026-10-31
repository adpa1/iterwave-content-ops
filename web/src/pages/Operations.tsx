import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { useUrlTab } from "../nav";
import DemoBadge from "../components/DemoBadge";
import EmptyHint from "../components/EmptyHint";
import AutogrowPanel from "../components/AutogrowPanel";
import OpsTargetsEditor from "../components/OpsTargetsEditor";
import { NotifyChannelsEditor } from "../components/ConfigEditors";
import {
  api, Platform, CircuitBreakerEvent, Draft,
  OpsKpiResponse, OpsLogItem, TaskRunItem, NotificationItem,
  SchedulerJob, RhythmConfig, NotifyRule, SystemConfigView,
} from "../api";

const PLATFORM_NAME: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音",
  kuaishou: "快手", wechat_channels: "视频号",
};

const ACTION_LABEL: Record<string, string> = {
  passive_reply: "被动回复", active_comment: "主动评论", follow: "关注", browse: "浏览",
};

const STATUS_LABEL: Record<string, string> = {
  success: "成功", failed: "失败", skipped: "跳过", circuit_broken: "熔断拦截",
  pending: "待执行", running: "执行中", interrupted: "中断",
  pending_verify: "待核实",
};

const DRAFT_STATUS_LABEL: Record<string, string> = {
  draft_pending: "待存草稿", draft_saved: "已存草稿", draft_failed: "存草稿失败", published: "已发布",
  collecting: "数据采集中", collected: "已采集", expired: "已过期", cancelled: "已取消", failed: "失败",
};

const STATUS_CLS: Record<string, string> = {
  success: "good", failed: "bad", skipped: "warn", circuit_broken: "bad",
  pending: "", running: "info", interrupted: "warn", pending_verify: "purple",
};

const LEVEL_CLS: Record<string, string> = {
  info: "", warning: "orange", error: "red", critical: "red",
};

type OpsPane = "kpi" | "logs" | "publish" | "breaker" | "tasks" | "scheduler" | "rhythm" | "notifications";
type OpsGroup = "kpi" | "publish" | "guard" | "jobs" | "inbox";
type OpsTab = OpsGroup | OpsPane;

/** 8 个子面板合并为 5 个页签；旧链接（?tab=scheduler 等）自动落到所属页签 */
const TABS: { key: OpsGroup; label: string; panes: OpsPane[] }[] = [
  { key: "kpi", label: "运营总控", panes: ["kpi"] },
  { key: "publish", label: "发布确认", panes: ["publish"] },
  { key: "guard", label: "熔断与节奏", panes: ["breaker", "rhythm"] },
  { key: "jobs", label: "任务与定时", panes: ["tasks", "scheduler"] },
  { key: "inbox", label: "日志与通知", panes: ["logs", "notifications"] },
];
const ALL_OPS_TABS: OpsTab[] = [...TABS.map((t) => t.key), "logs", "breaker", "tasks", "scheduler", "rhythm", "notifications"];
function groupOf(t: OpsTab): OpsGroup {
  return (TABS.find((g) => g.key === t || g.panes.includes(t as OpsPane)) ?? TABS[0]).key;
}

export default function Operations() {
  const { showToast, withPending } = useApp();
  const [rawTab, setTab] = useUrlTab<OpsTab>(ALL_OPS_TABS, "kpi");
  const tab = groupOf(rawTab);
  const show = (p: OpsPane) => (TABS.find((g) => g.key === tab)?.panes ?? []).includes(p);

  // 各子页数据源
  const [kpi, setKpi] = useState<OpsKpiResponse | null>(null);
  const [logs, setLogs] = useState<OpsLogItem[]>([]);
  const [tasks, setTasks] = useState<TaskRunItem[]>([]);
  const [notifs, setNotifs] = useState<NotificationItem[]>([]);
  const [notifRules, setNotifRules] = useState<NotifyRule[]>([]);
  const [jobs, setJobs] = useState<{ jobs: SchedulerJob[]; running: boolean } | null>(null);
  const [rhythm, setRhythm] = useState<RhythmConfig | null>(null);
  const [cb, setCb] = useState<{ events: CircuitBreakerEvent[]; broken_platforms: Platform[] } | null>(null);
  const [draftSb, setDraftSb] = useState<string>("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** KPI 展示窗口（1=今日 / 7 / 30 / 90），与 R12 评分 90 天口径独立 */
  const [kpiDays, setKpiDays] = useState(1);
  const [notifBusy, setNotifBusy] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        show("kpi") || show("logs") ? api.opsKpi(kpiDays) : Promise.resolve(null),
        show("logs") ? api.opsLogs(undefined, undefined, undefined, 50) : Promise.resolve(null),
        show("tasks") ? api.taskRuns(undefined, undefined, 50) : Promise.resolve(null),
        show("notifications") ? api.notifications(undefined, 30) : Promise.resolve(null),
        show("notifications") ? api.notifyRules() : Promise.resolve(null),
        show("scheduler") ? api.schedulerJobs() : Promise.resolve(null),
        show("rhythm") ? api.rhythm() : Promise.resolve(null),
        show("breaker") ? api.circuitBreaker() : Promise.resolve(null),
      ]);
      const unpack = (r: PromiseSettledResult<any>) => r.status === "fulfilled" ? r.value : null;
      setKpi(unpack(results[0]));
      const logsRes = unpack(results[1]);
      setLogs(logsRes?.items ?? []);
      setTasks(unpack(results[2]) ?? []);
      setNotifs(unpack(results[3]) ?? []);
      setNotifRules(unpack(results[4]) ?? []);
      setJobs(unpack(results[5]));
      setRhythm(unpack(results[6]));
      setCb(unpack(results[7]));
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
          <div className="eyebrow">运营控制台</div>
          <h1 className="page-title">运营控制台</h1>
          <div className="page-desc">
            监控运营动作、发布确认、熔断与节奏、定时任务和通知；系统只存草稿，发布永远由你确认。
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {/* 全局软暂停：只停运营动作（评论/关注），存草稿与数据采集不受影响 */}
          <button
            className="btn"
            disabled
            title="平台运营动作尚未接入，当前没有可暂停或恢复的评论/关注任务"
          >
            运营动作尚未接入
          </button>
          <button className="btn" onClick={testNotify} disabled={notifBusy}>
            {notifBusy ? "自检中…" : "♧ 测试通知"}
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
            <span>! 接口异常：{loadError}</span>
            <button className="btn sm" onClick={refresh}>↻ 重试</button>
          </div>
        </div>
      )}

      {/* 子 tab bar */}
      <div className="tabs page-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${tab === t.key ? "active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* === 1. 运营总控（KPI）=== */}
      {show("kpi") && <KpiPane kpi={kpi} days={kpiDays} onDaysChange={setKpiDays} />}

      {/* === 2. 评论日志 === */}
      {show("logs") && <LogsPane logs={logs} />}

      {/* === 3. 发布确认 === */}
      {show("publish") && <PublishPane />}

      {/* === 4. 熔断器 === */}
      {show("breaker") && (
        <BreakerPane cb={cb} onRecover={async (p) => {
          // G-11：同一平台的恢复/重置在请求中不可重复触发
          try {
            const r = await withPending(`cb:recover:${p}`, () => api.recoverCb(p));
            if (r) showToast(r.passed ? "✓ 恢复通过" : `✕ ${r.reason}`, r.passed ? "success" : "error");
          } catch (e: any) { showToast(e.message, "error"); }
          await refresh();
        }} onReset={async (p) => {
          try {
            await withPending(`cb:reset:${p}`, () => api.resetCb(p));
            showToast(`⚙ 已重置 ${p}`);
          } catch (e: any) { showToast(e.message, "error"); }
          await refresh();
        }} />
      )}

      {/* === 5. 任务追踪 === */}
      {show("tasks") && <TasksPane tasks={tasks} onResolved={refresh} />}

      {/* === 6. 定时任务 === */}
      {show("scheduler") && <SchedulerPane jobs={jobs} onTriggered={refresh} />}

      {/* === 7. 节奏调度 === */}
      {show("rhythm") && <RhythmPane rhythm={rhythm} onSaved={refresh} />}

      {/* === 8. 通知中心 === */}
      {show("notifications") && <NotificationsPane items={notifs} rules={notifRules} onChanged={refresh} />}
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
        <DemoBadge level="demo" text="评论与关注等平台运营动作尚未接入；下方仅展示系统历史记录，不会实际执行平台操作" />
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
        <KpiCard label={`${rangeLabel}运营动作`} value={todayTotalOps} hint={`${rangeLabel}已执行的运营动作`} />
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
                  {s.circuit_broken ? <span className="tag red">! 熔断</span> : <span className="tag green">正常</span>}
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
                  <div className="task-title">{DRAFT_STATUS_LABEL[status] ?? status}</div>
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
          <div className="panel-title"><span className="icon">≋</span>运营日志</div>
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
  // 已发布笔记链接：被动评论轮询用它定位笔记；不填则该篇跳过轮询
  const [noteUrls, setNoteUrls] = useState<Record<string, string>>({});
  // 实际发布已发生但质检失败/失效时，要求显式说明例外原因，保留数据闭环与审计记录。
  const [qualityOverrideReasons, setQualityOverrideReasons] = useState<Record<string, string>>({});
  const [platformSyncReasons, setPlatformSyncReasons] = useState<Record<string, string>>({});
  const [platformSyncOverrideReasons, setPlatformSyncOverrideReasons] = useState<Record<string, string>>({});

  const fetchDrafts = async () => {
    try {
      const all = await api.drafts();
      // draft_pending 代表仅保存在本地的干跑/待存内容；用户可以复制到平台手动发布后在这里登记。
      setDrafts(all.filter((d) =>
        d.status === "draft_pending" || d.status === "draft_saved" ||
        d.status === "user_published" || d.status === "data_fetching" || d.status === "published"
      ));
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
      const noteUrl = (noteUrls[id] ?? "").trim();
      const overrideReason = (qualityOverrideReasons[id] ?? "").trim();
      const platformSyncOverrideReason = (platformSyncOverrideReasons[id] ?? "").trim();
      const res = await withPending(
        `publish:${id}`,
        () => api.confirmPublish(
          id, noteUrl || undefined, overrideReason || undefined,
          platformSyncOverrideReason || undefined,
        ),
      );
      if (res) {
        // 行内即时更新，无需等整表刷新
        setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, status: res.status } : d)));
        showToast(
          platformSyncOverrideReason
            ? "✓ 已登记发布；平台版本差异原因已留痕"
            : overrideReason
              ? "✓ 已登记发布；质检/审核例外原因已留痕"
              : "✓ 已标记 user_published",
          "success",
        );
        await fetchDrafts();
      }
    } catch (e: any) {
      setError(e?.message ?? "确认发布失败");
      showToast(`确认失败: ${e?.message ?? "未知错误"}`, "error");
    } finally {
      setPending(null);
    }
  };

  const confirmPlatformSync = async (id: string) => {
    if (pending) return;
    const reason = (platformSyncReasons[id] ?? "").trim();
    if (!reason) {
      showToast("请先填写平台版本同步说明", "error");
      return;
    }
    setPending(id);
    setError(null);
    try {
      const result = await withPending(
        `platform-sync:${id}`,
        () => api.confirmPlatformSync(id, reason),
      );
      if (result) {
        showToast(result.message, "success");
        setPlatformSyncReasons((prev) => ({ ...prev, [id]: "" }));
        await fetchDrafts();
      }
    } catch (e: any) {
      setError(e?.message ?? "确认平台版本同步失败");
      showToast(`确认平台版本同步失败：${e?.message ?? "未知错误"}`, "error");
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">◷</span>发布确认双通道</div>
          <div className="panel-sub">手动点「✓ 我已发布」或等系统每 30 分钟自动检测</div>
          <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
            <DemoBadge level="blocked" text="系统永不点击发布按钮，只能由你手动发布" />
          </div>
        </div>
      </div>
      {error && (
        <div className="note" style={{ color: "var(--red)", marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
          <span>! {error}</span>
          <button className="btn sm" onClick={fetchDrafts}>重试</button>
        </div>
      )}
      {drafts.length === 0 ? (
        <EmptyHint next="可勾选「执行存草稿」后在此确认；干跑内容可复制到平台手动发布，再回来登记">暂无待确认发布的内容</EmptyHint>
      ) : (
        <div className="list">
          {drafts.map((d) => (
            <div key={d.id} className="task-row">
              <i className={`task-prio ${d.status === "user_published" ? "green" : "orange"}`} />
              <div className="task-main">
                <div className="task-title">{PLATFORM_NAME[d.platform as Platform]} · {d.title ?? d.id}</div>
                <div className="task-desc">
                  状态：<strong>{d.status}</strong>
                  {d.status === "draft_pending"
                    ? " · 仅保存在 IterWave，本内容尚未存入平台"
                    : d.saved_at ? ` · 存草稿 ${new Date(d.saved_at).toLocaleString("zh-CN")}` : ""}
                </div>
                {d.status === "draft_pending" && (
                  <div className="note" style={{ marginTop: 6 }}>
                    这是干跑/待存内容。请在「内容生产 → 内容编辑与审核」复制文案，手动发布到平台后再回来登记；系统不会自动发布。
                  </div>
                )}
                {d.review_rejected && (d.status === "draft_pending" || d.status === "draft_saved") && (
                  <div className="note" style={{ marginTop: 6, color: "var(--red)" }}>
                    人工审核已驳回：{d.review_rejection_reason || "未提供原因"}。正常发布流程已阻断；
                    请在内容生产页修改并重新提交审核。若该篇事实上已在平台发布，必须填写例外原因才能登记真实发布。
                  </div>
                )}
                {(d.status === "draft_pending" || d.status === "draft_saved") &&
                  d.guard_confirmation_allowed !== true && !d.review_rejected && (
                  <div className="note" style={{ marginTop: 6, color: "var(--orange)" }}>
                    当前质检状态：{d.guard_outcome ?? "未检查"}{d.guard_reason ? ` · ${d.guard_reason}` : ""}。
                    尚未发布时请先回到内容审核页复检；若已在平台真实发布，需填写例外原因才能登记采集。
                  </div>
                )}
                {d.status === "draft_saved" && d.platform_content_sync !== "matched" && (
                  <div className="note" style={{ marginTop: 6, color: "var(--orange)" }}>
                    {d.platform_content_sync === "stale"
                      ? "本地文案与上次存入平台的版本不同。发布前请先在平台端同步标题、正文、标签和卡片。"
                      : "该历史平台草稿没有可验证的版本指纹，需要人工核对平台端内容。"}
                    {" "}若已发布的确是平台旧版本，需填写版本例外原因才能登记并进入数据采集。
                  </div>
                )}
              </div>
              <div className="task-actions" style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
                {(d.status === "draft_pending" || d.status === "draft_saved") && (
                  <>
                    <input
                      value={noteUrls[d.id] ?? d.platform_note_url ?? ""}
                      onChange={(e) => setNoteUrls((prev) => ({ ...prev, [d.id]: e.target.value }))}
                      placeholder="已发布笔记链接（可选；用于定位采集）"
                      style={{ width: 240, fontSize: 12 }}
                      disabled={pending !== null}
                    />
                    {d.guard_confirmation_allowed !== true && (
                      <input
                        value={qualityOverrideReasons[d.id] ?? ""}
                        onChange={(e) => setQualityOverrideReasons((prev) => ({ ...prev, [d.id]: e.target.value }))}
                        placeholder={d.review_rejected
                          ? "若已发布但被驳回，填写例外原因（必填）"
                          : "若已发布但质检未过，填写例外原因（必填）"}
                        style={{ width: 260, fontSize: 12 }}
                        disabled={pending !== null}
                        aria-label={`${d.title ?? d.id} 的发布质检例外原因`}
                      />
                    )}
                    {d.status === "draft_saved" && d.platform_content_sync !== "matched" && (
                      <>
                        <input
                          value={platformSyncReasons[d.id] ?? ""}
                          onChange={(e) => setPlatformSyncReasons((prev) => ({ ...prev, [d.id]: e.target.value }))}
                          placeholder="尚未发布：说明已在平台同步为当前版本（必填）"
                          style={{ width: 280, fontSize: 12 }}
                          disabled={pending !== null}
                          aria-label={`${d.title ?? d.id} 的平台版本同步说明`}
                        />
                        <button
                          className="btn sm"
                          disabled={
                            pending !== null ||
                            !d.guard_confirmation_allowed ||
                            !(platformSyncReasons[d.id] ?? "").trim()
                          }
                          onClick={() => confirmPlatformSync(d.id)}
                          title="仅在你已手动把平台草稿更新为当前本地版本后点击"
                        >
                          已在平台同步当前版本
                        </button>
                        <input
                          value={platformSyncOverrideReasons[d.id] ?? ""}
                          onChange={(e) => setPlatformSyncOverrideReasons((prev) => ({ ...prev, [d.id]: e.target.value }))}
                          placeholder="已发布的是旧版：填写例外原因（必填）"
                          style={{ width: 280, fontSize: 12 }}
                          disabled={pending !== null}
                          aria-label={`${d.title ?? d.id} 的平台版本例外原因`}
                        />
                      </>
                    )}
                    <button
                      className="btn sm success"
                      disabled={
                        pending !== null ||
                        (d.guard_confirmation_allowed !== true && !(qualityOverrideReasons[d.id] ?? "").trim()) ||
                        (d.status === "draft_saved" && d.platform_content_sync !== "matched" &&
                          !(platformSyncOverrideReasons[d.id] ?? "").trim())
                      }
                      onClick={() => confirm(d.id)}
                    >
                      {pending === d.id
                        ? "提交中…"
                        : d.status === "draft_pending"
                          ? (d.guard_confirmation_allowed === true ? "✓ 我已手动发布" : "⚠ 已发布，仍登记")
                          : d.platform_content_sync !== "matched"
                            ? "⚠ 已发布，版本不一致仍登记"
                            : d.guard_confirmation_allowed === true
                              ? "✓ 我已发布"
                              : "⚠ 已发布，仍登记"}
                    </button>
                  </>
                )}
                {(d.status === "user_published" || d.status === "data_fetching" || d.status === "published") && (
                  <span className="tag green">已进入采集/复盘流程</span>
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
                <span className={`tag ${broken ? "red" : "green"}`}>{broken ? "! 熔断中" : "✓ 正常"}</span>
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
          <div className="panel-title"><span className="icon">⌁</span>任务追踪</div>
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
                      <button className="btn sm" disabled={busyId === t.id} onClick={() => resolve(t.id, "sent")}>✓ 已发出</button>
                      <button className="btn sm ghost" style={{ marginLeft: 6 }} disabled={busyId === t.id} onClick={() => resolve(t.id, "not_sent")}>✕ 未发出</button>
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
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ enabled: boolean; mode: "interval" | "cron"; interval: string; cron: string }>(
    { enabled: true, mode: "interval", interval: "", cron: "" },
  );
  if (!jobs) return <div className="note">加载中…</div>;

  const runNow = async (j: SchedulerJob) => {
    setBusyId(j.id);
    setMsg(null);
    try {
      await api.runSchedulerJob(j.id);
      setMsg(`已触发「${j.name}」，可到「任务追踪」查看结果`);
      onTriggered?.();
    } catch (e: any) {
      setMsg(`触发失败: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const startEdit = (j: SchedulerJob) => {
    setEditing(j.id);
    setDraft({
      enabled: j.enabled,
      mode: j.cron ? "cron" : "interval",
      interval: j.interval_minutes != null ? String(j.interval_minutes) : "",
      cron: j.cron ?? "",
    });
  };

  const save = async (j: SchedulerJob) => {
    const prefix = `scheduler.jobs.${j.id}`;
    const updates: Record<string, unknown> = {};
    if (draft.enabled !== j.enabled) updates[`${prefix}.enabled`] = draft.enabled;
    if (draft.mode === "interval") {
      const n = Number(draft.interval);
      if (!Number.isInteger(n) || n < 1) { setMsg("间隔必须是正整数（分钟）"); return; }
      if (n !== j.interval_minutes || j.cron) updates[`${prefix}.interval_minutes`] = n;
    } else {
      const c = draft.cron.trim();
      if (c.split(/\s+/).length !== 5) { setMsg("cron 必须是 5 段：分 时 日 月 周"); return; }
      if (c !== j.cron) updates[`${prefix}.cron`] = c;
    }
    if (Object.keys(updates).length === 0) { setEditing(null); return; }
    setBusyId(j.id);
    try {
      await api.patchConfig(updates);
      setMsg(`「${j.name}」已保存并立即重排`);
      setEditing(null);
      onTriggered?.();
    } catch (e: any) {
      setMsg(`保存失败: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const active = jobs.jobs.filter((j) => j.enabled).length;
  return (
    <>
      <AutogrowPanel />
      <div className="grid three" style={{ marginBottom: 16 }}>
        <div className="metric">
          <div className="metric-label">定时调度状态</div>
          <div className="metric-val" style={{ color: jobs.running ? "var(--green)" : "var(--red)" }}>
            {jobs.running ? "运行中" : "已停止"}
          </div>
          <div className="metric-foot">{active} / {jobs.jobs.length} 个任务已启用</div>
        </div>
      </div>
      <div className="panel">
        <div className="panel-head">
          <div className="panel-title"><span className="icon">◷</span>定时任务（{jobs.jobs.length}）</div>
          <div className="panel-sub">开关与频率全部来自配置，修改后写入覆盖文件并立即重排，无需重启</div>
        </div>
        {msg && (
          <div className="panel-sub" role="status" style={{ padding: "0 16px 8px", color: "var(--subtle)" }}>{msg}</div>
        )}
        <div className="list">
          {jobs.jobs.length === 0 && (
            <EmptyHint next="确认后端服务已启动后刷新">没有定时任务</EmptyHint>
          )}
          {jobs.jobs.map((j) => (
            <div key={j.id} className="task-row" style={{ flexWrap: "wrap" }}>
              <i className={`task-prio ${j.enabled ? "blue" : "gray"}`} />
              <div className="task-main">
                <div className="task-title">
                  {j.name} <code style={{ fontSize: 10, color: "var(--muted)" }}>{j.id}</code>
                  {!j.enabled && <span className="tag gray" style={{ marginLeft: 6 }}>已关闭</span>}
                </div>
                <div className="task-desc">
                  {j.schedule}
                  {j.enabled && j.next_run_time && <> · 下次 {new Date(j.next_run_time).toLocaleString("zh-CN")}</>}
                  {j.enabled && !j.next_run_time && <> · 未排定</>}
                </div>
              </div>
              {j.configurable && editing !== j.id && (
                <button className="btn sm" onClick={() => startEdit(j)}>配置</button>
              )}
              <button
                className="btn sm"
                disabled={busyId === j.id || !j.enabled}
                onClick={() => runNow(j)}
                title={j.enabled ? "立即触发一次（不影响后续排期）" : "任务已关闭"}
              >
                {busyId === j.id ? "处理中…" : "▶ 立即执行"}
              </button>
              {editing === j.id && (
                <form
                  className="flex wrap"
                  style={{ gap: 8, width: "100%", padding: "10px 0 2px 18px", alignItems: "center" }}
                  onSubmit={(e) => { e.preventDefault(); save(j); }}
                >
                  <label className="flex" style={{ gap: 4, alignItems: "center" }}>
                    <input type="checkbox" checked={draft.enabled}
                      onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
                    启用
                  </label>
                  <label className="sr-only" htmlFor={`mode-${j.id}`}>触发方式</label>
                  <select id={`mode-${j.id}`} className="field" value={draft.mode}
                    onChange={(e) => setDraft({ ...draft, mode: e.target.value as "interval" | "cron" })}>
                    <option value="interval">固定间隔</option>
                    <option value="cron">cron 表达式</option>
                  </select>
                  {draft.mode === "interval" ? (
                    <label className="flex" style={{ gap: 4, alignItems: "center" }}>
                      每
                      <input className="field" type="number" min={1} max={10080} style={{ width: 90 }}
                        value={draft.interval} onChange={(e) => setDraft({ ...draft, interval: e.target.value })} />
                      分钟
                    </label>
                  ) : (
                    <label className="flex" style={{ gap: 4, alignItems: "center" }}>
                      <span className="sr-only">cron</span>
                      <input className="field" style={{ width: 160, fontFamily: "monospace" }} placeholder="0 8 * * *"
                        value={draft.cron} onChange={(e) => setDraft({ ...draft, cron: e.target.value })} />
                      <span style={{ fontSize: 11, color: "var(--muted)" }}>分 时 日 月 周（本地时区）</span>
                    </label>
                  )}
                  <button type="submit" className="btn sm primary" disabled={busyId === j.id}>保存</button>
                  <button type="button" className="btn sm" onClick={() => setEditing(null)}>取消</button>
                </form>
              )}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function RhythmPane({ rhythm, onSaved }: { rhythm: RhythmConfig | null; onSaved?: () => void }) {
  const { showToast } = useApp();
  const [quotas, setQuotas] = useState<Record<string, Record<string, string>>>({});
  const [opsStart, setOpsStart] = useState("");
  const [opsEnd, setOpsEnd] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!rhythm) return;
    const next: Record<string, Record<string, string>> = {};
    for (const p of rhythm.enabled_platforms) {
      const q = rhythm.quotas[p];
      next[p] = {
        passive_replies: String(q?.passive_replies ?? ""),
        active_comments: String(q?.active_comments ?? ""),
        follows: String(q?.follows ?? ""),
      };
    }
    setQuotas(next);
    setOpsStart(rhythm.ops_window ? String(rhythm.ops_window.start_hour) : "");
    setOpsEnd(rhythm.ops_window ? String(rhythm.ops_window.end_hour) : "");
  }, [rhythm]);

  if (!rhythm) return <div className="note">加载中…</div>;

  const save = async () => {
    const updates: Record<string, unknown> = {};
    for (const p of rhythm.enabled_platforms) {
      for (const field of ["passive_replies", "active_comments", "follows"] as const) {
        const raw = quotas[p]?.[field] ?? "";
        const n = Number(raw);
        if (raw === "" || !Number.isInteger(n) || n < 0) {
          showToast(`${PLATFORM_NAME[p]} 的 ${field} 必须是非负整数`, "error");
          return;
        }
        if (n !== rhythm.quotas[p]?.[field]) updates[`rhythm.daily_quotas.${p}.${field}`] = n;
      }
    }
    const start = Number(opsStart);
    const end = Number(opsEnd);
    if (opsStart !== "" && (!Number.isInteger(start) || start < 0 || start > 23)) {
      showToast("运营时段开始小时必须是 0-23", "error"); return;
    }
    if (opsEnd !== "" && (!Number.isInteger(end) || end < 0 || end > 23)) {
      showToast("运营时段结束小时必须是 0-23", "error"); return;
    }
    if (start >= end) { showToast("开始小时必须小于结束小时", "error"); return; }
    if (rhythm.ops_window && start !== rhythm.ops_window.start_hour) {
      updates["rhythm.windows.ops.start_hour"] = start;
    }
    if (rhythm.ops_window && end !== rhythm.ops_window.end_hour) {
      updates["rhythm.windows.ops.end_hour"] = end;
    }
    if (Object.keys(updates).length === 0) { showToast("节奏配置没有改动", "info"); return; }
    setBusy(true);
    try {
      await api.patchConfig(updates);
      showToast(`✓ 已保存 ${Object.keys(updates).length} 项节奏配置`, "success");
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid two">
      {rhythm.enabled_platforms.map((p) => (
        <div key={p} className="panel">
          <div className="panel-head">
            <div className="panel-title"><span className="icon">♪</span>{PLATFORM_NAME[p]} · 节奏配置</div>
            <div className="head-actions"><span className="tag gray">平台独立配额</span></div>
          </div>
          <div className="grid three" style={{ gap: 8 }}>
            {([
              ["passive_replies", "被动回复/日"],
              ["active_comments", "主动评论/日"],
              ["follows", "关注/日"],
            ] as const).map(([field, label]) => (
              <div key={field}>
                <label className="form-label">{label}</label>
                <input className="field" type="number" min={0} style={{ width: "100%" }}
                  value={quotas[p]?.[field] ?? ""}
                  onChange={(e) => setQuotas((prev) => ({
                    ...prev, [p]: { ...prev[p], [field]: e.target.value },
                  }))} />
              </div>
            ))}
          </div>
          <div className="note" style={{ marginTop: 8, fontSize: 11 }}>
            发布时段：{rhythm.posting_windows[p]?.join(" / ") ?? "-"}（跟随下方全局运营时段）
          </div>
        </div>
      ))}
      <div className="panel" style={{ gridColumn: "1 / -1" }}>
        <div className="panel-head">
          <div className="panel-title"><span className="icon">≋</span>全局运营时段</div>
          <div className="panel-sub">所有平台共用；本地时区</div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
          <div>
            <label className="form-label">开始（0-23）</label>
            <input className="field" type="number" min={0} max={23} style={{ width: 80 }}
              value={opsStart} onChange={(e) => setOpsStart(e.target.value)} />
          </div>
          <div>
            <label className="form-label">结束（0-23）</label>
            <input className="field" type="number" min={0} max={23} style={{ width: 80 }}
              value={opsEnd} onChange={(e) => setOpsEnd(e.target.value)} />
          </div>
          <button className="btn primary sm" onClick={save} disabled={busy}>
            {busy ? "保存中…" : "保存节奏配置"}
          </button>
          <span className="metric-foot">
            随机间隔 {rhythm.intervals?.passive_reply_base_seconds ?? "—"}s ± {rhythm.intervals?.passive_reply_jitter_seconds ?? "—"}s
            （可在「系统设置 → 高级参数」调整）
          </span>
        </div>
        {!rhythm.ops_window && (
          <div className="note" style={{ marginTop: 8, color: "var(--orange)" }}>
            ! 当前未配置 ops 时段窗口，节奏控制器按全天放行；建议设置 8 → 23。
          </div>
        )}
      </div>
      <OpsTargetsEditor />
    </div>
  );
}

function NotificationsPane({ items, rules, onChanged }: {
  items: NotificationItem[];
  rules: NotifyRule[];
  onChanged?: () => void;
}) {
  const { showToast } = useApp();
  const TYPE_LABEL: Record<string, string> = {
    draft_saved: "草稿已存", draft_failed: "存草稿失败",
    circuit_broken: "熔断触发", login_expired: "登录过期",
    goal_completed: "目标达成", data_fetch_failed: "数据采集失败",
    round_completed: "轮次完成", circuit_recovered: "熔断恢复",
    publish_confirmed: "发布确认", quota_exhausted: "配额耗尽",
    round_timeout: "轮次阶段超时",
  };
  const CHANNELS = ["macos", "wecom", "feishu"];
  const LEVELS = ["info", "warning", "error", "critical"];
  const [scenario, setScenario] = useState("circuit_broken");
  const [channel, setChannel] = useState("macos");
  const [level, setLevel] = useState("warning");
  const [busy, setBusy] = useState(false);
  const [cfgView, setCfgView] = useState<SystemConfigView | null>(null);

  useEffect(() => {
    api.config().then(setCfgView).catch(() => setCfgView(null));
  }, []);

  const addRule = async () => {
    setBusy(true);
    try {
      await api.createNotifyRule({ scenario, channel, level, enabled: true });
      showToast("✓ 通知规则已新增", "success");
      onChanged?.();
    } catch (e: any) {
      showToast(`新增失败: ${e?.message ?? e}`, "error");
    } finally { setBusy(false); }
  };

  const toggleRule = async (r: NotifyRule) => {
    try {
      await api.updateNotifyRule(r.id, {
        scenario: r.scenario, channel: r.channel, level: r.level, enabled: !r.enabled,
      });
      onChanged?.();
    } catch (e: any) { showToast(`更新失败: ${e?.message ?? e}`, "error"); }
  };

  const changeLevel = async (r: NotifyRule, next: string) => {
    try {
      await api.updateNotifyRule(r.id, {
        scenario: r.scenario, channel: r.channel, level: next, enabled: !!r.enabled,
      });
      onChanged?.();
    } catch (e: any) { showToast(`更新失败: ${e?.message ?? e}`, "error"); }
  };

  const removeRule = async (r: NotifyRule) => {
    try {
      await api.deleteNotifyRule(r.id);
      showToast("已删除规则", "success");
      onChanged?.();
    } catch (e: any) { showToast(`删除失败: ${e?.message ?? e}`, "error"); }
  };

  return (
    <>
      {cfgView && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⌁</span>通知通道</div>
              <div className="panel-sub">
                通道级总开关：关闭后该通道任何事件都不发（规则表也无法绕过）
              </div>
            </div>
          </div>
          <NotifyChannelsEditor
            view={cfgView}
            onSaved={() => api.config().then(setCfgView).catch(() => {})} />
        </div>
      )}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⚙</span>通知规则（notify_rules 表）</div>
            <div className="panel-sub">
              某场景一旦有规则，就只按规则限定的通道/级别发送；没有规则 → 走 config 默认
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 }}>
          <div>
            <label className="form-label">场景</label>
            <select className="field" value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {Object.entries(TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </div>
          <div>
            <label className="form-label">通道</label>
            <select className="field" value={channel} onChange={(e) => setChannel(e.target.value)}>
              {CHANNELS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="form-label">级别</label>
            <select className="field" value={level} onChange={(e) => setLevel(e.target.value)}>
              {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
          <button className="btn primary sm" onClick={addRule} disabled={busy}>＋ 新增规则</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>场景</th><th>通道</th><th>级别</th><th>启用</th><th style={{ width: 70 }}>操作</th></tr></thead>
            <tbody>
              {rules.length === 0 && (
                <tr><td colSpan={5}>
                  <EmptyHint className="" next="不配置时全部事件按默认规则发送；需要精细控制时再新增">暂无自定义规则</EmptyHint>
                </td></tr>
              )}
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{TYPE_LABEL[r.scenario] ?? r.scenario}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>{r.channel}</td>
                  <td>
                    <select className="field" style={{ width: 110 }} value={r.level}
                      onChange={(e) => changeLevel(r, e.target.value)}>
                      {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
                    </select>
                  </td>
                  <td>
                    <input type="checkbox" checked={!!r.enabled} onChange={() => toggleRule(r)} />
                  </td>
                  <td><button className="btn sm" onClick={() => removeRule(r)}>删除</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="panel-title"><span className="icon">♧</span>消息通知历史</div>
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
    </>
  );
}
