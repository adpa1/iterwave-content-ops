import { useEffect, useState } from "react";
import EmptyHint from "../components/EmptyHint";
import { api, Platform, SystemStatus, AiCallRecord, UserOverrideRecord, RedlineRecord } from "../api";

const PLATFORM_LABEL: Record<string, string> = {
  xhs: "📕 小红书", douyin: "🎵 抖音", kuaishou: "⚡ 快手", wechat_channels: "💚 视频号",
};

// 原型的流水线节点（G → G1 → G2 → H → I；后续平台独立运行 + 采集 + 复盘）
const PIPELINE = [
  { id: "G",    label: "G 选题评分",        skill: "topic_scoring",    gate: "三模式自动切换" },
  { id: "G1",   label: "G1 四平台变体",      skill: "prompt_variants",  gate: "同选题 × 四风格" },
  { id: "G2",   label: "G2 终极目标检查",    skill: "ultimate_goal_check", gate: "R2 不能绕过，不通过打回 G" },
  { id: "H",    label: "H 图片卡片制作",     skill: "image_card",       gate: "Pillow + OpenCV" },
  { id: "I",    label: "I Playwright 存草稿", skill: "draft_saver",      gate: "R1 永点存草稿，不点发布" },
  { id: "J",    label: "J 运营（评论/关注）", skill: "ops_engine",       gate: "软暂停可关；熔断 R6 拦截" },
  { id: "K",    label: "K 指标采集 + 复盘",   skill: "spiral_engine",    gate: "data_fetch 失败 3 次 / 30min" },
];

const SKILLS = [
  // 原型按「运营工作流」分组
  { group: "内容生产", rows: [
    { id: "topic_scoring",      name: "选题评分引擎",        src: "src/core/scoring/",                 status: "OK", note: "三模式自动切换" },
    { id: "prompt_variants",    name: "四平台变体生成",      src: "src/core/prompt_variants.py",       status: "OK", note: "G→G1 同选题×四风格" },
    { id: "ultimate_goal_check",name: "终极目标检查（R2）", src: "src/core/guard/",                    status: "OK", note: "三入口 check_content/strategy/comment" },
    { id: "image_card",         name: "图片卡片制作",        src: "src/adapters/image_card.py",        status: "OK", note: "Pillow + OpenCV" },
    { id: "draft_saver",        name: "Playwright 存草稿",  src: "src/adapters/draft_saver.py",       status: "⚠️", note: "R1：只点存草稿" },
  ]},
  { group: "运营与复盘", rows: [
    { id: "circuit_breaker",    name: "熔断器（R6）",       src: "src/core/ops/circuit_breaker.py",   status: "OK", note: "5 信号 + 3 步恢复" },
    { id: "rhythm_controller",  name: "节奏控制器",          src: "src/core/ops/rhythm.py",            status: "OK", note: "平台独立配额 + 时段窗口" },
    { id: "ops_engine",         name: "运营引擎",           src: "src/core/ops/engine.py",            status: "⚠️", note: "被动回复已接；主动评论/关注极低配额" },
    { id: "spiral_engine",      name: "螺旋引擎 + 缺口分析", src: "src/core/spiral/",                  status: "OK", note: "三通道输出" },
    { id: "zscore_normalize",   name: "跨平台归一化（R13）", src: "src/core/metrics/",                 status: "OK", note: "平台内 z-score" },
  ]},
  { group: "支撑层", rows: [
    { id: "recovery_manager",  name: "断点恢复（R16）",    src: "src/core/recovery.py",              status: "OK", note: "SQLiteJobStore + 幂等覆盖" },
    { id: "scheduler",          name: "APScheduler 7 类任务", src: "src/core/scheduler/",             status: "OK", note: "SQLiteJobStore 持久化" },
    { id: "platform_readback",  name: "平台数据读回",        src: "src/vendor/easel/",                 status: "⚠️", note: "account_stats / platform_readback 需登录态" },
  ]},
];

export default function Agent() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  // P1 — AI 审计链状态
  const [aiCalls, setAiCalls] = useState<AiCallRecord[] | null>(null);
  const [aiCallsError, setAiCallsError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<UserOverrideRecord[] | null>(null);
  const [overridesError, setOverridesError] = useState<string | null>(null);
  const [redlines, setRedlines] = useState<RedlineRecord[] | null>(null);
  const [redlinesError, setRedlinesError] = useState<string | null>(null);
  const [auditTab, setAuditTab] = useState<"ai" | "override" | "redline">("ai");

  const load = async () => {
    setError(null);
    try { setStatus(await api.status()); }
    catch (e: any) { setError(e.message); }
  };
  useEffect(() => { load(); }, []);

  const loadAudit = async () => {
    setAiCallsError(null); setOverridesError(null); setRedlinesError(null);
    try { setAiCalls(await api.aiCalls(200)); } catch (e: any) { setAiCallsError(e.message); }
    try { setOverrides(await api.userOverride(200)); } catch (e: any) { setOverridesError(e.message); }
    try { setRedlines(await api.redlineHistory(200)); } catch (e: any) { setRedlinesError(e.message); }
  };
  useEffect(() => { loadAudit(); }, []);

  const broken = status?.circuit_broken_platforms ?? [];
  const loginMissing = status?.platforms_without_login ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">AGENT ORCHESTRATION</div>
          <h1 className="page-title">Agent 编排与 Skills</h1>
          <div className="page-desc">
            原型 4 大模块：编排定义（静态可预览）· 端到端阻塞诊断 · 平台子流程追踪 · 工作流 Skills。
            MVP 不做多 Agent 链；RoundExecutor 是唯一主调度。
          </div>
        </div>
      </div>

      {error && (
        <div className="panel" style={{ borderColor: "var(--red)", marginBottom: 14 }}>
          <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>⚠️ /api/status 异常：{error}</span>
            <button className="btn sm" onClick={load}>↻ 重试</button>
          </div>
        </div>
      )}

      {/* ⚑ 端到端流程与阻塞诊断（原型核心） */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⚑</span>端到端流程与阻塞诊断</div>
            <div className="panel-sub">7 个阶段的编排定义（静态可预览）+ 真实系统信号诊断</div>
          </div>
        </div>

        <div className="list">
          {PIPELINE.map((n, idx) => {
            const isGate = idx === 2; // G2 终极目标检查
            const isPlaywright = idx === 4;
            return (
              <div key={n.id} className="task-row">
                <i className={`task-prio ${isGate ? "purple" : isPlaywright ? "red" : "blue"}`} />
                <div className="task-main">
                  <div className="task-title">
                    <strong>{n.label}</strong>
                    <span style={{ marginLeft: 10, fontSize: 11, color: "var(--subtle)", fontFamily: "monospace" }}>skill: {n.skill}</span>
                  </div>
                  <div className="task-desc">{n.gate}</div>
                </div>
                <div style={{ fontSize: 22, fontWeight: 800, color: idx === PIPELINE.length - 1 ? "var(--teal)" : "var(--blue)" }}>
                  {idx === PIPELINE.length - 1 ? "⇒" : "→"}
                </div>
              </div>
            );
          })}
        </div>

        {/* 真实系统信号 */}
        <div style={{ marginTop: 14 }} className="table-wrap">
          <table>
            <thead><tr><th>信号</th><th>值</th><th>判定</th></tr></thead>
            <tbody>
              <tr><td>熔断平台</td><td>{broken.length > 0 ? broken.map((p: Platform) => PLATFORM_LABEL[p]).join(" · ") : "—"}</td>
                <td>{broken.length > 0 ? <span className="tag red">🚨 阻塞</span> : <span className="tag green">OK</span>}</td></tr>
              <tr><td>缺登录态平台</td><td>{loginMissing.length > 0 ? loginMissing.map((p: Platform) => PLATFORM_LABEL[p]).join(" · ") : "—"}</td>
                <td>{loginMissing.length > 0 ? <span className="tag orange">⚠ 阻塞存草稿/采集</span> : <span className="tag green">OK</span>}</td></tr>
              <tr><td>评分模式</td><td>{status?.scoring_mode ?? "—"}</td><td><span className="tag blue">正常</span></td></tr>
              <tr><td>活跃轮次</td><td>{status?.active_rounds ?? 0}</td><td><span className="tag gray">{(status?.active_rounds ?? 0) > 0 ? "运行中" : "空闲"}</span></td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid two" style={{ marginBottom: 16 }}>
        {/* ☷ 平台子流程运行追踪 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">☷</span>平台子流程运行追踪</div>
              <div className="panel-sub">每个平台独立状态与子任务（R4 明确平台隔离）</div>
            </div>
          </div>
          {!status ? (
            <div className="note">加载中…</div>
          ) : status.enabled_platforms.length === 0 ? (
            <EmptyHint next="到「系统设置」启用至少 1 个平台">暂无启用的平台</EmptyHint>
          ) : (
            <div className="list">
              {status.enabled_platforms.map((p: Platform) => {
                const isBroken = broken.includes(p);
                const isNoLogin = loginMissing.includes(p);
                return (
                  <div key={p} className="task-row">
                    <i className={`task-prio ${isBroken ? "red" : isNoLogin ? "orange" : "green"}`} />
                    <div className="task-main">
                      <div className="task-title">{PLATFORM_LABEL[p]}</div>
                      <div className="task-desc">
                        {isBroken ? "🚨 已熔断 · 停止所有运营动作"
                         : isNoLogin ? "⚠ 无登录态 · 存草稿/采集/评论阻塞"
                         : "✓ 登录态 OK · 可执行全部平台交互"}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ♻️ 断点恢复 */}
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">♻️</span>断点恢复（SQLiteJobStore）</div>
              <div className="panel-sub">启动时自动补登记 · 幂等覆盖（R16）</div>
            </div>
          </div>
          {status?.recovery ? (
          <div className="list">
            <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">中断任务</div><div className="task-desc">{status.recovery.interrupted_tasks} 个已重登</div></div></div>
            <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">补登记采集</div><div className="task-desc">{status.recovery.requeued_data_fetch.length} 篇 draft 重入列</div></div></div>
            <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">卡住轮次</div><div className="task-desc">{status.recovery.stale_rounds.length > 0 ? `#${status.recovery.stale_rounds.join(", #")}` : "无"}</div></div></div>
            <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">待核实（pending_verify）</div><div className="task-desc">{status.recovery.pending_verify} 条主动运营崩溃场景</div></div></div>
          </div>
          ) : (
          <div className="note">加载中…</div>
          )}
        </div>
      </div>

      {/* ▧ 按运营工作流查看 Skills（原型核心） */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">▧</span>按运营工作流查看 Skills</div>
            <div className="panel-sub">{SKILLS.reduce((a, g) => a + g.rows.length, 0)} 个 Skills / {SKILLS.length} 个工作流分组（MVP 只读）</div>
          </div>
        </div>
        {SKILLS.map((g) => (
          <div key={g.group} style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: "var(--muted)", letterSpacing: ".5px", marginBottom: 6 }}>
              {g.group}
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>ID</th><th>名称</th><th>源码</th><th>状态</th><th>备注</th></tr></thead>
                <tbody>
                  {g.rows.map((s) => (
                    <tr key={s.id}>
                      <td style={{ fontSize: 12, fontFamily: "monospace" }}>{s.id}</td>
                      <td>{s.name}</td>
                      <td style={{ fontSize: 11, color: "var(--muted)", fontFamily: "monospace" }}>{s.src}</td>
                      <td><span className={`tag ${s.status === "OK" ? "green" : "orange"}`}>{s.status}</span></td>
                      <td style={{ fontSize: 12, color: "var(--muted)" }}>{s.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>

      {/* 📋 P1 — AI 证据审计链（对比报告缺口补齐） */}
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">📋</span>AI 证据审计链</div>
            <div className="panel-sub">LLM 调用记录 · 人工覆盖痕迹 · 终极目标红线拦截历史 — 原型 view-review → 缺口与 AI 证据 的 live 对应</div>
          </div>
          <button className="btn sm" onClick={loadAudit} style={{ marginLeft: 10 }}>↻ 刷新</button>
        </div>

        {/* Tab 切换 */}
        <div style={{ display: "flex", gap: 6, marginBottom: 12, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          {([
            { key: "ai", label: `① LLM 调用记录${aiCalls ? ` (${aiCalls.length})` : ""}` },
            { key: "override", label: `② 人工覆盖痕迹${overrides ? ` (${overrides.length})` : ""}` },
            { key: "redline", label: `③ 红线拦截历史${redlines ? ` (${redlines.length})` : ""}` },
          ] as const).map((t) => (
            <button
              key={t.key}
              onClick={() => setAuditTab(t.key)}
              style={{
                padding: "6px 14px",
                fontSize: 12,
                fontWeight: auditTab === t.key ? 700 : 500,
                background: auditTab === t.key ? "var(--blue)" : "transparent",
                color: auditTab === t.key ? "#fff" : "var(--text)",
                border: auditTab === t.key ? "1px solid var(--blue)" : "1px solid var(--border)",
                borderRadius: 6,
                cursor: "pointer",
              }}
            >{t.label}</button>
          ))}
        </div>

        {/* ① AI Calls */}
        {auditTab === "ai" && (
          <>
            {aiCallsError && <div className="note" style={{ color: "var(--red)" }}>加载失败：{aiCallsError}</div>}
            {!aiCalls && !aiCallsError && <div className="note">加载中…</div>}
            {aiCalls && aiCalls.length === 0 && (
              <EmptyHint next="启动真实 LLM 生成后自动记录">还没有真实 LLM 调用记录</EmptyHint>
            )}
            {aiCalls && aiCalls.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr>
                    <th>时间</th><th>Step</th><th>模型</th><th>Prompt Ver</th>
                    <th>Tokens In</th><th>Tokens Out</th><th>延迟</th><th>结果</th>
                  </tr></thead>
                  <tbody>
                    {aiCalls.slice(0, 100).map((r) => (
                      <tr key={r.id}>
                        <td style={{ fontSize: 11, fontFamily: "monospace", color: "var(--subtle)" }}>{r.called_at}</td>
                        <td style={{ fontSize: 12, fontFamily: "monospace" }}>{r.step}</td>
                        <td>{r.model}</td>
                        <td style={{ fontSize: 11, fontFamily: "monospace" }}>{r.prompt_version}</td>
                        <td>{r.tokens_in ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td>{r.tokens_out ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td>{r.latency_ms}ms</td>
                        <td>
                          {r.success ? <span className="tag green">OK</span> : <span className="tag red">FAIL</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {aiCalls.length > 100 && <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 6 }}>只显示最近 100 条（共 {aiCalls.length} 条）</div>}
              </div>
            )}
          </>
        )}

        {/* ② User Override */}
        {auditTab === "override" && (
          <>
            {overridesError && <div className="note" style={{ color: "var(--red)" }}>加载失败：{overridesError}</div>}
            {!overrides && !overridesError && <div className="note">加载中…</div>}
            {overrides && overrides.length === 0 && (
              <EmptyHint next="编辑内容时选择「驳回」或「重新过 G2」后自动留痕">还没有人工覆盖痕迹</EmptyHint>
            )}
            {overrides && overrides.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr>
                    <th>时间</th><th>轮次</th><th>字段</th><th>原值</th><th>新值</th><th>操作人</th><th>原因</th>
                  </tr></thead>
                  <tbody>
                    {overrides.slice(0, 100).map((r) => (
                      <tr key={r.id}>
                        <td style={{ fontSize: 11, fontFamily: "monospace", color: "var(--subtle)" }}>{r.overridden_at}</td>
                        <td>{r.round_id ? `#${r.round_id}` : <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td style={{ fontFamily: "monospace", fontSize: 11 }}>{r.override_field ?? "—"}</td>
                        <td style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.original_value ?? "—"}</td>
                        <td style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.override_value ?? "—"}</td>
                        <td>{r.overridden_by}</td>
                        <td style={{ maxWidth: 200, fontSize: 12 }}>{r.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}

        {/* ③ Redline History */}
        {auditTab === "redline" && (
          <>
            {redlinesError && <div className="note" style={{ color: "var(--red)" }}>加载失败：{redlinesError}</div>}
            {!redlines && !redlinesError && <div className="note">加载中…</div>}
            {redlines && redlines.length === 0 && (
              <EmptyHint next="选题或文案违反红线时会自动拦截并记录">还没有红线拦截历史 ✅</EmptyHint>
            )}
            {redlines && redlines.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr>
                    <th>时间</th><th>轮次</th><th>平台</th><th>标题</th><th>拦截类型</th><th>原因</th>
                  </tr></thead>
                  <tbody>
                    {redlines.slice(0, 100).map((r) => (
                      <tr key={r.id}>
                        <td style={{ fontSize: 11, fontFamily: "monospace", color: "var(--subtle)" }}>{r.created_at}</td>
                        <td>{r.round_id ? `#${r.round_id}` : "—"}</td>
                        <td>{PLATFORM_LABEL[r.platform] ?? r.platform}</td>
                        <td style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.title}</td>
                        <td>
                          {r.guard_outcome === "fail" ? <span className="tag red">❌ FAIL</span>
                           : r.guard_outcome === "warn" ? <span className="tag orange">⚠️ WARN</span>
                           : <span className="tag gray">{r.guard_outcome}</span>}
                        </td>
                        <td style={{ maxWidth: 240, fontSize: 12 }}>{r.guard_reason ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 6 }}>
                  💡 R2 终极目标检查器 → 违反账号定位 → 拦截打回；拦截历史永久审计可追溯
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
