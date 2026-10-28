import { useEffect, useState } from "react";
import { useApp } from "../AppContext";
import EmptyHint from "../components/EmptyHint";
import { api, type SystemStatus, type SystemConfigView } from "../api";

const PLATFORM_LABELS: Record<string, string> = {
  xhs: "小红书",
  douyin: "抖音",
  kuaishou: "快手",
  wechat_channels: "视频号",
};

const SCORING_MODE_LABELS: Record<string, string> = {
  cold_start: "冷启动（全库样本 < 10）",
  sample_accumulating: "样本积累（部分维度 < 5）",
  stable: "稳定运行（所有活跃维度 ≥ 5）",
};

export default function Settings() {
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
      showToast(`✅ 已保存 ${res.changed.length} 项配置，已写入 config_changes 审计`, "success");
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
          <div className="page-desc">config/config.yaml 为基线；下方「运行时配置」支持白名单字段在线修改，保存后立即生效并写入 config_changes 审计。</div>
        </div>
        <div className="head-actions">
          <button className="btn sm" onClick={load}>↻ 刷新</button>
        </div>
      </div>

      {/* G-13：运行时配置（白名单可写）*/}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⚙</span>运行时配置 · 白名单可编辑</div>
            <div className="panel-sub">
              只允许改配额 / 运营时段 / 熔断冷却 / 热点热度与时效门限；持久化到 {cfgView?.overrides_file ?? "data/config_overrides.json"}
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
                      <EmptyHint className="" next="检查 /api/config 是否返回 daily_quotas，或在 config/config.yaml 配置 rhythm.daily_quotas">暂无平台配额配置</EmptyHint>
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
              <span className="tag red">🔒 R22 维度匹配门：强制开启</span>
              <span className="tag red">🔒 R22 可发布性门：强制开启</span>
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
                        <td style={{ fontSize: 12 }}>{a.key}</td>
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
              <EmptyHint next="修改上方任一配额 / 运营时段 / 门限后点「保存配置」，变更会写入 config_changes 审计">
                暂无配置变更记录
              </EmptyHint>
            )}
          </>
        )}
      </div>

      {/* 本地知识库（G-13 / 原型 step 3） */}
      <div className="grid two" style={{ marginTop: 14 }}>
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">▣</span>连接本地知识库</div>
              <div className="panel-sub">数据源 + 字段映射 + 同步扫描策略</div>
            </div>
            <div className="head-actions">
              <button className="btn sm" onClick={async () => {
                try {
                  const r = await api.kbInspect();
                  showToast(r.ok ? `✅ ${r.stats?.total ?? 0} 条选题，${r.stats?.unique_dimensions ?? 0} 个维度` : `❌ ${r.error || "检查失败"}`, r.ok ? "success" : "error");
                } catch (e: any) { showToast(`检查失败: ${e?.message ?? e}`, "error"); }
              }}>🔍 诊断选题库</button>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
          <div className="list">
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">路径</div><div className="task-desc" style={{ fontFamily: "monospace", fontSize: 11 }}>{cfgView.readonly.kb_path}</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">数据源类型</div><div className="task-desc">{cfgView.readonly.kb_type} · 表 {cfgView.readonly.kb_table}</div></div></div>
            <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">字段映射</div><div className="task-desc" style={{ fontFamily: "monospace", fontSize: 11 }}>{Object.entries(cfgView.readonly.kb_field_mapping).length > 0 ? JSON.stringify(cfgView.readonly.kb_field_mapping) : "(默认自动)"}</div></div></div>
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">扫描策略</div><div className="task-desc">APScheduler kb_scan · 每天 06:00（config.scan_cron）</div></div></div>
          </div>
          )}
        </div>

        {/* 主模型与备用（原型 step 4） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⇄</span>主模型与备用</div>
              <div className="panel-sub">不暴露 API Key（R23）；密钥只存 .env</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
          <div className="list">
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">Provider</div><div className="task-desc">{cfgView.readonly.llm_provider}</div></div></div>
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">主模型</div><div className="task-desc">{cfgView.readonly.llm_model}</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">备用模型</div><div className="task-desc">{cfgView.readonly.llm_fallback_model || "未配置"}</div></div></div>
            <div className="task-row">
              <i className={`task-prio ${cfgView.readonly.llm_has_api_key ? "green" : "red"}`} />
              <div className="task-main">
                <div className="task-title">API Key 状态</div>
                <div className="task-desc">{cfgView.readonly.llm_has_api_key ? "✓ 已从 .env 读取（不明文回传）" : "✗ 未找到，Skills 无法执行真实调用"}</div>
              </div>
            </div>
          </div>
          )}
        </div>

        {/* 平台账号与连接（原型 step 2） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">◉</span>平台账号与连接</div>
              <div className="panel-sub">enabled 开关 + account_key（登录态文件 storage_state.json 另存）</div>
            </div>
          </div>
          {!cfgView ? (
            <div className="note">加载中…</div>
          ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>平台</th><th>启用</th><th>account_key</th><th>base_url</th></tr></thead>
              <tbody>
                {Object.entries(cfgView.readonly.platforms).map(([k, v]) => (
                  <tr key={k}>
                    <td>{PLATFORM_LABELS[k] ?? k}</td>
                    <td><span className={`tag ${v.enabled ? "green" : "gray"}`}>{v.enabled ? "✓" : "✗"}</span></td>
                    <td style={{ fontFamily: "monospace", fontSize: 11 }}>{v.account_key || "(未配置)"}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--muted)" }}>{v.base_url}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          )}
        </div>

        {/* 平台默认规则（原型「平台默认规则」panel） */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚙</span>平台默认规则</div>
              <div className="panel-sub">当前配置快照（不支持在线改，改 config/config.yaml 生效）</div>
            </div>
          </div>
          <div className="list">
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">R6 熔断冷却</div><div className="task-desc">{cfgView?.editable.cooldown_minutes ?? "—"} 分钟 · 3 步恢复验证</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">运营时段</div><div className="task-desc">{cfgView?.editable.ops_window.start_hour ?? "—"} → {cfgView?.editable.ops_window.end_hour ?? "—"}（本地时区）</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">发布确认超时</div><div className="task-desc">72h 主 + 48h grace；producing 48h 独立（R8）</div></div></div>
            <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">三模式权重</div><div className="task-desc">冷启动 / 样本积累 / 稳定运行（R11）</div></div></div>
          </div>
        </div>
      </div>

      <div className="grid two">
        {/* 运行配置 — 动态从 /api/status 读取 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚙</span>当前运行配置</div>
              <div className="panel-sub">实时从 /api/status 获取</div>
            </div>
          </div>
          {err ? (
            <div className="note" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
              <span>加载失败: {err}</span>
              <button className="btn sm" onClick={load}>↻ 重试</button>
            </div>
          ) : (
          <div className="list">
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">后端 API</div><div className="task-desc">http://127.0.0.1:8000</div></div></div>
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">已启用平台</div><div className="task-desc">{enabledNames}</div></div></div>
            <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">评分模式</div><div className="task-desc">{modeLabel}</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">已发布样本</div><div className="task-desc">{status ? status.published_samples : "—"} 条（选题 × 平台）</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">活跃轮次</div><div className="task-desc">{status ? status.active_rounds : "—"} 个进行中</div></div></div>
            {brokenNames && (
              <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">🚨 熔断平台</div><div className="task-desc" style={{ color: "var(--danger)" }}>{brokenNames}</div></div></div>
            )}
          </div>
          )}
        </div>

        {/* 断点恢复 — 动态 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">♻️</span>断点恢复状态</div>
              <div className="panel-sub">APScheduler SQLiteJobStore + 幂等覆盖</div>
            </div>
          </div>
          {status?.recovery ? (
          <div className="list">
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">中断任务</div><div className="task-desc">{status.recovery.interrupted_tasks} 个已重登</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">补登记采集</div><div className="task-desc">{status.recovery.requeued_data_fetch.length} 篇（{status.recovery.requeued_data_fetch.join(", ") || "无"}）</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">卡住轮次</div><div className="task-desc">{status.recovery.stale_rounds.length > 0 ? `#${status.recovery.stale_rounds.join(", #")}` : "无"}</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">待核实（pending_verify）</div><div className="task-desc">{status.recovery.pending_verify} 条（主动运营崩溃场景）</div></div></div>
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

        {/* 登录态管理 — 静态说明 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">🔐</span>登录态管理</div>
              <div className="panel-sub">storage_state.json（chmod 600，R23 安全）</div>
            </div>
          </div>
          <div className="note" style={{ marginBottom: 12 }}>
            登录态不在 Web UI 里扫码（浏览器跨域问题）。请在终端执行：
          </div>
          <pre style={{
            background: "var(--surface-2)", padding: 14, borderRadius: 8,
            fontSize: 12, fontFamily: "monospace", color: "var(--text)",
            overflow: "auto", marginBottom: 14,
          }}>
{`# 小红书扫码登录
source .venv/bin/activate
python -c "
import asyncio
from src.core.login_manager import login_with_qr
from src.adapters.base import Platform
asyncio.run(login_with_qr(Platform.XHS, 'https://creator.xiaohongshu.com/publish/publish'))
"`}
          </pre>
          <div className="note">登录成功后 storage_state.json 存到 data/login_states/，适配器自动恢复。</div>
        </div>

        {/* 红线规则 — 静态说明 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">🚨</span>红线规则（R1-R23）</div>
              <div className="panel-sub">所有模块必须遵守，违反 = 立即停手重写</div>
            </div>
          </div>
          <div className="list">
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">R1 永不自动点发布按钮</div><div className="task-desc">Playwright 只存草稿，发布 = 用户手动确认</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">R3 NULL ≠ 0</div><div className="task-desc">指标字段无 DEFAULT 0；未知进度 = NULL</div></div></div>
            <div className="task-row"><i className="task-prio red" /><div className="task-main"><div className="task-title">R6 熔断 3 步恢复</div><div className="task-desc">冷却到期 + test_login + test_lightweight</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">R13 z-score 平台内部</div><div className="task-desc">不同平台 +1σ 不代表相同业务表现</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">R22 4 道筛选门</div><div className="task-desc">维度匹配 + 可发布性（不可关）；热度 + 时效（可调）</div></div></div>
          </div>
        </div>
      </div>
    </>
  );
}

/** 配置值 → 输入框字符串；NULL 显示空串（不填 0，R3） */
function _str(v: number | null | undefined): string {
  return v == null ? "" : String(v);
}
