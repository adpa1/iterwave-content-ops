import { useEffect, useState } from "react";
import { navigate, useUrlTab } from "../nav";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import TenantManagerPane from "../components/TenantManagerPane";
import RuntimePolicyEditor from "../components/RuntimePolicyEditor";
import { AdvancedEditor, KbEditor, LlmEditor, PlatformStyleEditor, PlatformToggles, TimeoutEditor } from "../components/ConfigEditors";
import { api, type SystemStatus, type SystemConfigView } from "../api";

const PLATFORM_LABELS: Record<string, string> = {
  xhs: "小红书",
  douyin: "抖音",
  kuaishou: "快手",
  wechat_channels: "视频号",
};

const SCORING_MODE_LABELS: Record<string, string> = {
  cold_start: "冷启动",
  sample_accumulating: "样本积累",
  stable: "稳定运行",
};

const SETTINGS_TABS = [
  { k: "runtime", l: "运行与配额" },
  { k: "style", l: "平台文案风格" },
  { k: "model", l: "知识库与模型" },
  { k: "platform", l: "平台与账号" },
  { k: "rhythm", l: "节奏与评分权重" },
  { k: "policy", l: "运行策略参数" },
  { k: "system", l: "运行状态" },
  { k: "tenants", l: "账号组" },
] as const;
type SettingsTab = typeof SETTINGS_TABS[number]["k"];

export default function Settings() {
  const [tab, setTab] = useUrlTab<SettingsTab>(SETTINGS_TABS.map((t) => t.k), "runtime");
  const { showToast, withPending, isPending } = useApp();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cfgView, setCfgView] = useState<SystemConfigView | null>(null);
  // 运行时配置表单（只保留白名单字段，保存时按改动差量提交）
  const [quotas, setQuotas] = useState<Record<string, Record<string, string>>>({});
  const [opsWindow, setOpsWindow] = useState<{ start: string; end: string }>({ start: "", end: "" });
  const [cooldown, setCooldown] = useState("");
  const [hotFilters, setHotFilters] = useState({
    heat: "", heatOn: true, hours: "", timeOn: true,
  });

  const load = async () => {
    setErr(null);
    try {
      setStatus(await api.status());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    await loadConfig();
  };

  /** G-13：运行时配置来自 /api/config（白名单字段 + 审计） */
  const loadConfig = async () => {
    try {
      const v = await api.config();
      setCfgView(v);
      const q: Record<string, Record<string, string>> = {};
      for (const [p, row] of Object.entries(v.editable.daily_quotas)) {
        q[p] = {
          passive_replies: _str(row.passive_replies),
          active_comments: _str(row.active_comments),
          follows: _str(row.follows),
        };
      }
      setQuotas(q);
      setOpsWindow({
        start: _str(v.editable.ops_window.start_hour),
        end: _str(v.editable.ops_window.end_hour),
      });
      setCooldown(_str(v.editable.cooldown_minutes));
      setHotFilters({
        heat: _str(v.editable.hot_topics.heat_threshold),
        heatOn: v.editable.hot_topics.heat_filter_enabled ?? true,
        hours: _str(v.editable.hot_topics.time_window_hours),
        timeOn: v.editable.hot_topics.time_filter_enabled ?? true,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => { load(); }, []);

  /** 只提交真正改动的字段（后端按白名单校验，越权 400） */
  const saveConfig = async () => {
    if (!cfgView) return;
    const updates: Record<string, unknown> = {};
    const ed = cfgView.editable;
    for (const [p, row] of Object.entries(quotas)) {
      for (const f of ["passive_replies", "active_comments", "follows"] as const) {
        const val = Number(row[f]);
        if (row[f] !== "" && val !== ed.daily_quotas[p]?.[f]) {
          updates[`rhythm.daily_quotas.${p}.${f}`] = val;
        }
      }
    }
    if (opsWindow.start !== "" && Number(opsWindow.start) !== ed.ops_window.start_hour) {
      updates["rhythm.windows.ops.start_hour"] = Number(opsWindow.start);
    }
    if (opsWindow.end !== "" && Number(opsWindow.end) !== ed.ops_window.end_hour) {
      updates["rhythm.windows.ops.end_hour"] = Number(opsWindow.end);
    }
    if (cooldown !== "" && Number(cooldown) !== ed.cooldown_minutes) {
      updates["circuit_breaker.cooldown_minutes"] = Number(cooldown);
    }
    if (hotFilters.heat !== "" && Number(hotFilters.heat) !== ed.hot_topics.heat_threshold) {
      updates["hot_topics.filters.heat_threshold"] = Number(hotFilters.heat);
    }
    if (hotFilters.heatOn !== (ed.hot_topics.heat_filter_enabled ?? true)) {
      updates["hot_topics.filters.heat_filter_enabled"] = hotFilters.heatOn;
    }
    if (hotFilters.hours !== "" && Number(hotFilters.hours) !== ed.hot_topics.time_window_hours) {
      updates["hot_topics.filters.time_window_hours"] = Number(hotFilters.hours);
    }
    if (hotFilters.timeOn !== (ed.hot_topics.time_filter_enabled ?? true)) {
      updates["hot_topics.filters.time_filter_enabled"] = hotFilters.timeOn;
    }

    if (Object.keys(updates).length === 0) {
      showToast("没有需要保存的改动", "info");
      return;
    }
    try {
      const res = await withPending("config:save", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✓ 已保存 ${res.changed.length} 项配置`, "success");
      await loadConfig();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  };

  const enabledNames = status?.enabled_platforms
    .map((p) => PLATFORM_LABELS[p] ?? p)
    .join(" · ") ?? "加载中…";

  const brokenNames = status?.circuit_broken_platforms
    .map((p) => PLATFORM_LABELS[p] ?? p)
    .join(" · ") ?? "";

  const modeLabel = status
    ? SCORING_MODE_LABELS[status.scoring_mode] ?? status.scoring_mode
    : "加载中…";

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">SYSTEM SETTINGS</div>
          <h1 className="page-title">系统设置</h1>
          <div className="page-desc">所有设置保存后立即生效，并记录变更历史；按分类在下方页签中修改。</div>
        </div>
        <div className="head-actions">
          <button className="btn sm" onClick={load}>↻ 刷新</button>
        </div>
      </div>

      <div className="tabs" style={{ marginBottom: 16, alignSelf: "flex-start", display: "inline-flex" }}>
        {SETTINGS_TABS.map((t) => (
          <button key={t.k} type="button" className={`tab ${tab === t.k ? "active" : ""}`} onClick={() => setTab(t.k)}>{t.l}</button>
        ))}
      </div>

      {/* G-13：运行时配置（白名单可写）*/}
      {tab === "runtime" && (
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⚙</span>运行与配额</div>
            <div className="panel-sub">
              每日配额、运营时段、熔断冷却与热点门限；保存后立即生效
            </div>
          </div>
          <div className="head-actions">
            <button className="btn primary sm" disabled={!cfgView || isPending("config:save")} onClick={saveConfig}>
              {isPending("config:save") ? "保存中…" : "保存配置"}
            </button>
          </div>
        </div>

        {!cfgView ? (
          <div className="note">加载配置中…</div>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>平台</th><th>被动回复/日</th><th>主动评论/日</th><th>关注/日</th></tr>
                </thead>
                <tbody>
                  {Object.entries(quotas).length === 0 && (
                    <tr><td colSpan={4}>
                      <EmptyHint className="" next="在配置文件中设置各平台每日配额后刷新">暂无平台配额配置</EmptyHint>
                    </td></tr>
                  )}
                  {Object.entries(quotas).map(([p, row]) => (
                    <tr key={p}>
                      <td>{PLATFORM_LABELS[p] ?? p}</td>
                      {(["passive_replies", "active_comments", "follows"] as const).map((f) => (
                        <td key={f}>
                          <input
                            className="field"
                            type="number"
                            min={0}
                            value={row[f]}
                            style={{ width: 90 }}
                            onChange={(e) => setQuotas((prev) => ({ ...prev, [p]: { ...prev[p], [f]: e.target.value } }))}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="grid three" style={{ marginTop: 14 }}>
              <div className="metric">
                <div className="metric-label">运营时段（本地时区）</div>
                <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 6 }}>
                  <input className="field" type="number" min={0} max={23} value={opsWindow.start}
                    style={{ width: 70 }}
                    onChange={(e) => setOpsWindow((w) => ({ ...w, start: e.target.value }))} />
                  <span style={{ color: "var(--muted)" }}>→</span>
                  <input className="field" type="number" min={0} max={23} value={opsWindow.end}
                    style={{ width: 70 }}
                    onChange={(e) => setOpsWindow((w) => ({ ...w, end: e.target.value }))} />
                  <span className="metric-foot">时</span>
                </div>
              </div>
              <div className="metric">
                <div className="metric-label">熔断冷却（分钟）</div>
                <input className="field" type="number" min={1} max={1440} value={cooldown}
                  style={{ width: 100, marginTop: 6 }}
                  onChange={(e) => setCooldown(e.target.value)} />
              </div>
              <div className="metric">
                <div className="metric-label">热点：热度门限 / 时效窗口</div>
                <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 6 }}>
                  <input className="field" type="number" min={0} value={hotFilters.heat}
                    style={{ width: 90 }}
                    onChange={(e) => setHotFilters((f) => ({ ...f, heat: e.target.value }))} />
                  <input className="field" type="number" min={1} max={720} value={hotFilters.hours}
                    style={{ width: 70 }}
                    onChange={(e) => setHotFilters((f) => ({ ...f, hours: e.target.value }))} />
                  <span className="metric-foot">小时</span>
                </div>
                <div style={{ display: "flex", gap: 14, marginTop: 8, fontSize: 12 }}>
                  <label><input type="checkbox" checked={hotFilters.heatOn}
                    onChange={(e) => setHotFilters((f) => ({ ...f, heatOn: e.target.checked }))} /> 启用热度门</label>
                  <label><input type="checkbox" checked={hotFilters.timeOn}
                    onChange={(e) => setHotFilters((f) => ({ ...f, timeOn: e.target.checked }))} /> 启用时效门</label>
                </div>
              </div>
            </div>

            <div className="note" style={{ marginTop: 12, display: "flex", gap: 10, flexWrap: "wrap" }}>
              <span className="tag red">维度匹配：强制开启</span>
              <span className="tag red">可发布性检查：强制开启</span>
              <span style={{ color: "var(--muted)" }}>
                这两道门没有关闭入口；直接调 API 关闭会被后端拒绝（400）。
              </span>
            </div>

            {cfgView.audit.length > 0 && (
              <div className="table-wrap" style={{ marginTop: 14 }}>
                <table>
                  <thead><tr><th>#</th><th>配置项</th><th>旧值</th><th>新值</th><th>时间</th></tr></thead>
                  <tbody>
                    {cfgView.audit.map((a) => (
                      <tr key={a.id}>
                        <td style={{ color: "var(--subtle)", fontSize: 11 }}>{a.id}</td>
                        <td style={{ fontSize: 12 }} title={a.key}>{configKeyLabel(a.key)}</td>
                        <td style={{ color: "var(--muted)" }}>{a.old_value ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td>{a.new_value ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td style={{ fontSize: 11, color: "var(--muted)" }}>{a.changed_at ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {cfgView.audit.length === 0 && (
              <EmptyHint next="修改上方任一配额 / 运营时段 / 门限后点「保存配置」，这里会记录每次变更">
                暂无配置变更记录
              </EmptyHint>
            )}
          </>
        )}
      </div>

      )}

      {/* 平台文案风格（platform_styles 表，migrations/016）*/}
      {tab === "style" && (
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">✎</span>平台文案风格</div>
            <div className="panel-sub">
标题风格、正文结构、语气与话题标签；下一次生成文案时立即生效
            </div>
          </div>
        </div>
        <PlatformStyleEditor />
      </div>
      )}

      {/* 本地知识库：可编辑 */}
      {tab === "model" && (
      <div className="grid two">
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">▣</span>连接本地知识库</div>
              <div className="panel-sub">数据源 + 字段映射 + 同步扫描策略（可在线改，无需重启）</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
            <KbEditor view={cfgView} onSaved={loadConfig} />
          )}
        </div>

        {/* 主模型与备用：可编辑；Key 只写 .env */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⇄</span>主模型与备用</div>
              <div className="panel-sub">模型可在线切换；API Key 保存后不再回显</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
            <LlmEditor view={cfgView} onSaved={loadConfig} />
          )}
        </div>

      </div>
      )}

      {tab === "platform" && (
      <div className="grid two">
        {/* 平台账号与连接：启用开关可编辑 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">◉</span>平台账号与连接</div>
              <div className="panel-sub">启用或停用平台；登录在「账号与登录」页完成</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
            <PlatformToggles
              view={cfgView}
              onSaved={() => { loadConfig(); load(); }}
              loggedIn={status?.logged_in_platforms}
            />
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">▣</span>登录状态</div>
              <div className="panel-sub">扫码登录、过期提醒与登录记录统一在「账号与登录」页管理</div>
            </div>
          </div>
          <div className="list">
            {(["xhs", "douyin", "kuaishou", "wechat_channels"] as const).map((p) => {
              const on = status?.logged_in_platforms?.includes(p);
              return (
                <div className="task-row" key={p}>
                  <i className={`task-prio ${on ? "green" : "orange"}`} />
                  <div className="task-main"><div className="task-title">{PLATFORM_LABELS[p]}</div></div>
                  <span className={`tag ${on ? "green" : "gray"}`}>{on ? "已登录" : "未登录"}</span>
                </div>
              );
            })}
          </div>
          <button type="button" className="btn sm" style={{ marginTop: 12 }} onClick={() => navigate("accounts")}>去扫码登录 →</button>
        </div>
      </div>
      )}

      {tab === "platform" && cfgView && (
        <AccessNotifyPanel view={cfgView} onSaved={loadConfig} />
      )}

      {tab === "rhythm" && (
      <div className="grid two">
        {/* 阶段超时（R8：每个参数只管自己的阶段）— 可编辑 */}
        <div className="panel" style={{ gridColumn: "1 / -1" }}>
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚙</span>阶段超时与节奏参数</div>
              <div className="panel-sub">每个阶段独立计时，超时后进入人工处理</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
            <TimeoutEditor view={cfgView} onSaved={loadConfig} />
          )}
        </div>

        {/* 高级参数：评分权重 / 半衰期 / 节奏间隔 / 红线词库（安全底线仍硬锁） */}
        <div className="panel" style={{ gridColumn: "1 / -1" }}>
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⌁</span>评分权重、节奏间隔与红线词库</div>
              <div className="panel-sub">
安全底线不可关闭：维度匹配、可发布性检查，以及系统永不自动点发布
              </div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
            <AdvancedEditor view={cfgView} onSaved={loadConfig} />
          )}
        </div>
      </div>
      )}

      {tab === "policy" && <RuntimePolicyEditor />}

      {tab === "system" && (
      <div className="grid two">
        {/* 运行配置 — 动态从 /api/status 读取 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚙</span>当前运行配置</div>
              <div className="panel-sub">实时读取</div>
            </div>
          </div>
          {err ? (
            <div className="note" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
              <span>加载失败: {err}</span>
              <button className="btn sm" onClick={load}>↻ 重试</button>
            </div>
          ) : (
          <div className="list">
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">后端服务</div><div className="task-desc">{status ? "运行中" : "连接中…"}</div></div></div>
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">已启用平台</div><div className="task-desc">{enabledNames}</div></div></div>
            <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">评分模式</div><div className="task-desc">{modeLabel}</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">已发布样本</div><div className="task-desc">{status ? status.published_samples : "—"} 条</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">活跃轮次</div><div className="task-desc">{status ? status.active_rounds : "—"} 个进行中</div></div></div>
            {brokenNames && (
              <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">! 熔断平台</div><div className="task-desc" style={{ color: "var(--danger)" }}>{brokenNames}</div></div></div>
            )}
          </div>
          )}
        </div>

        {/* 断点恢复 — 动态 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">↻</span>断点恢复状态</div>
              <div className="panel-sub">服务重启后自动恢复中断的任务</div>
            </div>
          </div>
          {status?.recovery ? (
          <div className="list">
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">中断任务</div><div className="task-desc">{status.recovery.interrupted_tasks} 个已重登</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">补登记采集</div><div className="task-desc">{status.recovery.requeued_data_fetch.length} 篇（{status.recovery.requeued_data_fetch.join(", ") || "无"}）</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">卡住轮次</div><div className="task-desc">{status.recovery.stale_rounds.length > 0 ? `#${status.recovery.stale_rounds.join(", #")}` : "无"}</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">待核实</div><div className="task-desc">{status.recovery.pending_verify} 条</div></div></div>
          </div>
          ) : err ? (
          <div className="note" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>加载失败: {err}</span>
            <button className="btn sm" onClick={load}>↻ 重试</button>
          </div>
          ) : (
          <div className="note">加载中…</div>
          )}
        </div>

        {/* 红线规则 — 静态说明 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">!</span>安全红线</div>
              <div className="panel-sub">系统内置、不可关闭</div>
            </div>
          </div>
          <div className="list">
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">永不自动发布</div><div className="task-desc">系统只保存草稿，发布必须由你确认</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">未知不等于 0</div><div className="task-desc">没采集到的数据显示「待采集」，不当作 0 计算</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">熔断后三步恢复</div><div className="task-desc">冷却到期、登录检测、轻量操作检测全部通过才恢复</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">平台数据分开比较</div><div className="task-desc">各平台只和自己的历史比，不跨平台横比</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">热点四道筛选</div><div className="task-desc">维度匹配与可发布性不可关；热度与时效门限可调</div></div></div>
          </div>
        </div>
      </div>
      )}
      {tab === "tenants" && <TenantManagerPane />}

    </>
  );
}

/** 配置值 → 输入框字符串；NULL 显示空串（不填 0，R3） */
/** 配置键 → 中文名（变更记录用；未收录的保留原键） */
const KEY_PART: Record<string, string> = {
  ultimate_goal: "账号画像", persona_keywords: "定位关键词", forbidden_directions: "禁止方向", redline_keywords: "红线词库",
  positioning: "定位", goal_mode: "目标模式", platform_personas: "平台画像", platform_links: "平台协同",
  inherit_global: "继承全局", account_name: "账号名称", audience: "受众", role: "角色", extra_redlines: "额外红线",
  rhythm: "节奏", daily_quotas: "每日配额", passive_replies: "被动回复", active_comments: "主动评论", follows: "关注",
  windows: "时段", ops: "运营", draft_save: "存草稿", start_hour: "开始", end_hour: "结束",
  circuit_breaker: "熔断", cooldown_minutes: "冷却分钟", hot_topics: "热点", filters: "门限",
  heat_threshold: "热度门槛", heat_filter_enabled: "热度门开关", time_window_hours: "时效窗口", time_filter_enabled: "时效门开关",
  scheduler: "定时任务", jobs: "", enabled: "启用", interval_minutes: "间隔分钟", cron: "执行时间",
  kb_scan: "选题库扫描", daily_ops: "每日运营", hot_topics_refresh: "热点采集", autogrow: "自动闭环",
  platforms: "平台", xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
  llm: "模型", model: "模型", provider: "服务商", timeouts: "超时", scoring: "评分权重", topic_kb: "选题库", path: "文件位置",
};
function configKeyLabel(key: string): string {
  return key.split(".").map((p) => KEY_PART[p] ?? p).filter(Boolean).join(" · ");
}

function _str(v: number | null | undefined): string {
  return v == null ? "" : String(v);
}

/** 通知与访问：改口令 / 退出登录 / 企微飞书 Webhook（URL 只存 .env，不回显） */
function AccessNotifyPanel({ view, onSaved }: { view: SystemConfigView; onSaved: () => void }) {
  const { auth, refreshAuth, showToast } = useApp();
  const [password, setPassword] = useState("");
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [wecom, setWecom] = useState("");
  const [feishu, setFeishu] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const channels = view.readonly.notification_channels || {};
  const macEnabled = Boolean(channels.macos?.enabled);
  const macAvailable = channels.macos?.available !== false;

  const changePassword = async () => {
    if (password.trim().length < 6) {
      showToast("访问口令至少 6 个字符", "error");
      return;
    }
    setBusy("password");
    try {
      await api.setPassword(password, bootstrapToken);
      setPassword("");
      setBootstrapToken("");
      showToast("访问口令已更新，请重新登录", "success");
      await refreshAuth();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const logout = async () => {
    await api.authLogout();
    await refreshAuth();
  };

  const saveChannel = async (channel: "wecom" | "feishu") => {
    const url = channel === "wecom" ? wecom : feishu;
    if (!url.trim()) {
      showToast("请填写 Webhook 地址", "error");
      return;
    }
    setBusy(channel);
    try {
      await api.saveWebhook(channel, url.trim());
      (channel === "wecom" ? setWecom : setFeishu)("");
      showToast(`${channel === "wecom" ? "企微" : "飞书"} Webhook 已保存到 .env`, "success");
      onSaved();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="panel" style={{ marginTop: 18 }}>
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">◈</span>通知与访问</div>
          <div className="panel-sub">访问口令保护页面和接口；Webhook 地址只写入 .env，保存后不再回显</div>
        </div>
      </div>

      <div className="grid two">
        <div>
          <div className="field-label">访问口令</div>
          <div className="note" style={{ marginBottom: 8 }}>
            {auth?.configured
              ? "已设置口令，接口需要登录后才能调用"
              : "首次设置仅允许本机直连；公网域名或反向代理部署需先在服务器配置 ITERWAVE_BOOTSTRAP_TOKEN，再在此输入引导口令。"}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <input
              className="field" type="password" placeholder="新口令（至少 6 位）"
              value={password} onChange={(e) => setPassword(e.target.value)}
            />
            {!auth?.configured && (
              <input
                className="field" type="password" placeholder="首次设置引导口令（公网部署）"
                value={bootstrapToken} onChange={(e) => setBootstrapToken(e.target.value)}
              />
            )}
            <button className="btn sm" type="button" disabled={busy === "password"} onClick={changePassword}>
              {auth?.configured ? "修改口令" : "设置口令"}
            </button>
            {auth?.configured && (
              <button className="btn sm soft" type="button" onClick={logout}>退出登录</button>
            )}
          </div>
        </div>

        <div>
          <div className="field-label">消息通知</div>
          <div className="note" style={{ marginBottom: 8 }}>
            {macAvailable
              ? `macOS 通知${macEnabled ? "已开启" : "未开启"}；跨平台部署请用 Webhook`
              : "当前系统不支持 macOS 通知，请使用企微或飞书 Webhook"}
          </div>
          {(["wecom", "feishu"] as const).map((channel) => {
            const info = channels[channel];
            const value = channel === "wecom" ? wecom : feishu;
            const setValue = channel === "wecom" ? setWecom : setFeishu;
            return (
              <div key={channel} style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
                <input
                  className="field" style={{ flex: 1, minWidth: 160 }}
                  placeholder={channel === "wecom" ? "企微机器人 Webhook" : "飞书机器人 Webhook"}
                  value={value} onChange={(e) => setValue(e.target.value)}
                />
                <button className="btn sm soft" type="button"
                  disabled={busy === channel} onClick={() => saveChannel(channel)}>
                  {info?.has_webhook_url ? "更新" : "保存"}
                </button>
                <span className={info?.has_webhook_url ? "pill ok" : "pill"}>
                  {info?.has_webhook_url ? "已配置" : "未配置"}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

