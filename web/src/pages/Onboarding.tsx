import { useEffect, useState } from "react";
import { api, Platform, SystemConfigView, SystemStatus, Goal } from "../api";

const STEPS = [
  { key: "persona",  label: "① 账号画像",     hint: "终极目标 / 红线 / 定位关键词" },
  { key: "platforms", label: "② 启用平台",    hint: "4 选 2 起；建议先 1 平台跑通" },
  { key: "kb",       label: "③ 选题库 KB",    hint: "选题来源：SQLite / CSV / JSONL" },
  { key: "llm",      label: "④ 模型配置",     hint: "Provider / Model / API Key" },
  { key: "skills",   label: "⑤ Skills 环境",  hint: "检查登录态 / Playwright / 依赖" },
  { key: "goals",    label: "⑥ 阶段目标",     hint: "R21 口径：指标 / 目标值 / 基线" },
  { key: "check",    label: "⑦ 检查清单",     hint: "预检 + 启动首轮" },
] as const;

type StepKey = typeof STEPS[number]["key"];

const PLATFORM_ICONS: Record<Platform, string> = {
  xhs: "📕", douyin: "🎵", kuaishou: "⚡", wechat_channels: "💚",
};
const PLATFORM_NAMES: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
};

export default function Onboarding() {
  const [stepIdx, setStepIdx] = useState(0);
  const [cfg, setCfg] = useState<SystemConfigView | null>(null);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // 向导表单（本地状态，不直接写后端）
  const [persona, setPersona] = useState({ keywords: "", forbidden: "", redlines: "" });
  const [enabledPlatforms, setEnabledPlatforms] = useState<Set<Platform>>(new Set());

  const step: StepKey = STEPS[stepIdx].key;

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const [c, s, g] = await Promise.all([api.config(), api.status(), api.goals()]);
        setCfg(c);
        setStatus(s);
        setGoals(g);
        // 回填 persona
        setPersona({
          keywords: c.readonly.persona_keywords.join(", "),
          forbidden: c.readonly.forbidden_directions.join(", "),
          redlines: `${c.readonly.redline_keywords_count} 条已内置`,
        });
        setEnabledPlatforms(new Set(s.enabled_platforms));
      } catch (e: any) {
        setErr(e.message);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <div className="note">加载配置中…</div>;
  if (err) return <div className="note" style={{ color: "var(--red)" }}>加载失败：{err}</div>;
  if (!cfg || !status) return null;

  // 预检项
  const checks = {
    persona_ok: cfg.readonly.persona_keywords.length > 0,
    platforms_ok: status.enabled_platforms.length >= 1,
    kb_ok: !!(cfg.readonly.kb_path && cfg.readonly.kb_type),
    llm_ok: cfg.readonly.llm_has_api_key,
    login_ok: status.logged_in_platforms.length >= 1,
    goals_ok: goals.length >= 1,
  };
  const allOk = Object.values(checks).every(Boolean);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">FIRST-TIME SETUP</div>
          <h1 className="page-title">首次配置向导</h1>
          <div className="page-desc">
            7 步完成 IterWave 初始化。配置散落在「系统设置」，这里提供向导式入口。
            <span style={{ marginLeft: 8, color: "var(--orange)" }}>⚠ 向导只做引导和预检；实际写配置走「系统设置」</span>
          </div>
        </div>
      </div>

      {/* 进度条 */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
          {STEPS.map((s, i) => (
            <div
              key={s.key}
              onClick={() => i <= stepIdx && setStepIdx(i)}
              style={{
                flex: "1 1 120px",
                padding: "10px 12px",
                borderRadius: 8,
                cursor: i <= stepIdx ? "pointer" : "default",
                background: i === stepIdx ? "var(--blue)" : i < stepIdx ? "rgba(66,99,235,0.1)" : "var(--card)",
                color: i === stepIdx ? "#fff" : i < stepIdx ? "var(--blue)" : "var(--muted)",
                border: `1px solid ${i === stepIdx ? "var(--blue)" : "var(--border)"}`,
                fontSize: 12,
                transition: "all .15s",
              }}
            >
              <div style={{ fontWeight: 700 }}>{s.label}</div>
              <div style={{ fontSize: 10, opacity: 0.8, marginTop: 2 }}>{s.hint}</div>
              {i < stepIdx && <div style={{ fontSize: 16, marginTop: 4 }}>✓</div>}
            </div>
          ))}
        </div>
      </div>

      {/* Step 内容 */}
      <div className="panel" style={{ minHeight: 320 }}>
        {step === "persona" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>① 账号画像 · 终极目标</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              账号定位关键词 + 禁区。这些决定了 R2 终极目标检查器拦截什么内容。
            </p>
            <div style={{ display: "grid", gap: 14 }}>
              <FormRow label="定位关键词（逗号分隔）" value={persona.keywords} onChange={(v) => setPersona({ ...persona, keywords: v })} placeholder="如：AI 教育, 家长启蒙, 中小学, 工具效率" />
              <FormRow label="禁区方向（逗号分隔）" value={persona.forbidden} onChange={(v) => setPersona({ ...persona, forbidden: v })} placeholder="如：成人内容, 纯娱乐无教育价值" />
              <div>
                <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>红线敏感词库</div>
                <div className="tag blue">已内置 {cfg.readonly.redline_keywords_count} 条</div>
                <span style={{ fontSize: 11, color: "var(--subtle)", marginLeft: 8 }}>（R22 硬锁，不可关闭）</span>
              </div>
            </div>
            <div className="note" style={{ marginTop: 14 }}>
              💡 修改方式：到 <strong>系统设置</strong> → <strong>运行时配额</strong> 编辑 persona 字段
            </div>
          </div>
        )}

        {step === "platforms" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>② 启用平台</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              至少启用 1 个平台才能跑通首轮。建议先 1 平台验证，再扩。
            </p>
            <div className="grid two">
              {(Object.keys(PLATFORM_ICONS) as Platform[]).map((p) => {
                const realEnabled = status.enabled_platforms.includes(p);
                const loggedIn = status.logged_in_platforms.includes(p);
                const broken = status.circuit_broken_platforms.includes(p);
                return (
                  <div key={p} style={{
                    padding: 14, borderRadius: 10, border: `1px solid ${realEnabled ? "var(--blue)" : "var(--border)"}`,
                    background: realEnabled ? "rgba(66,99,235,0.06)" : "var(--card)",
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <div style={{ fontSize: 16, fontWeight: 700 }}>{PLATFORM_ICONS[p]} {PLATFORM_NAMES[p]}</div>
                      <div>
                        {realEnabled ? <span className="tag green">✅ 已启用</span> : <span className="tag gray">未启用</span>}
                      </div>
                    </div>
                    <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <span className={`tag ${loggedIn ? "green" : "orange"}`}>{loggedIn ? "🔑 已登录" : "🔒 无登录态"}</span>
                      {broken && <span className="tag red">🚨 已熔断</span>}
                      {cfg.readonly.platforms[p]?.account_key && <span className="tag blue">account_key: {cfg.readonly.platforms[p].account_key}</span>}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="note" style={{ marginTop: 14 }}>
              💡 启用 / 登录态切换到 <strong>系统设置</strong> → <strong>平台连接</strong>
            </div>
          </div>
        )}

        {step === "kb" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>③ 选题库 Knowledge Base</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              选题来源：本地 SQLite / CSV / JSONL。必须有 title + dimension 两个核心字段。
            </p>
            <div className="grid two">
              <InfoRow label="KB 类型" value={cfg.readonly.kb_type || "—"} />
              <InfoRow label="文件路径" value={cfg.readonly.kb_path || "—"} mono />
              <InfoRow label="数据表" value={cfg.readonly.kb_table || "—"} />
              <InfoRow label="字段映射" value={Object.entries(cfg.readonly.kb_field_mapping).length ? Object.entries(cfg.readonly.kb_field_mapping).map(([k, v]) => `${k}→${v}`).join(", ") : "—"} mono />
            </div>
            <div className="note" style={{ marginTop: 14 }}>
              ⚠ KB 路径在 config.yaml 定义，修改后需要重启后端。字段映射诊断到 <strong>内容资产</strong> → <strong>选题库</strong> 页面查看。
            </div>
          </div>
        )}

        {step === "llm" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>④ 模型配置</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              Provider + Model + API Key（存 <code>.env</code>，不进前端明文，R23）。
            </p>
            <div className="grid two">
              <InfoRow label="Provider" value={cfg.readonly.llm_provider || "—"} />
              <InfoRow label="主模型" value={cfg.readonly.llm_model || "—"} />
              <InfoRow label="兜底模型" value={cfg.readonly.llm_fallback_model || "—"} />
              <InfoRow label="API Key" value={cfg.readonly.llm_has_api_key ? "✅ 已配置（不明文回传）" : "❌ 未配置"} />
            </div>
            {!cfg.readonly.llm_has_api_key && (
              <div className="note" style={{ marginTop: 12, color: "var(--orange)" }}>
                ⚠ API Key 未配置 → 选题评分 + 文案生成 + 终极目标检查 全部不可用。请在 <code>config/.env</code> 写入后重启。
              </div>
            )}
          </div>
        )}

        {step === "skills" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>⑤ Skills 环境预检</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              核心运行依赖一次性检查：登录态 / 熔断 / 评分模式 / 活跃轮次 / 断点恢复。
            </p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>信号</th><th>值</th><th>状态</th></tr></thead>
                <tbody>
                  <tr><td>评分模式</td><td>{status.scoring_mode}</td><td><span className="tag green">✅</span></td></tr>
                  <tr><td>活跃轮次</td><td>{status.active_rounds}</td><td><span className={`tag ${status.active_rounds > 0 ? "blue" : "gray"}`}>{status.active_rounds > 0 ? "运行中" : "空闲（正常）"}</span></td></tr>
                  <tr><td>已发布样本</td><td>{status.published_samples}</td><td><span className={`tag ${status.published_samples >= 10 ? "green" : "orange"}`}>{status.published_samples >= 10 ? "样本充足" : "冷启动中"}</span></td></tr>
                  <tr><td>熔断平台</td><td>{status.circuit_broken_platforms.length > 0 ? status.circuit_broken_platforms.join(", ") : "—"}</td><td>{status.circuit_broken_platforms.length > 0 ? <span className="tag red">🚨 阻塞</span> : <span className="tag green">OK</span>}</td></tr>
                  <tr><td>缺登录态平台</td><td>{status.platforms_without_login.length > 0 ? status.platforms_without_login.map((p) => PLATFORM_NAMES[p]).join(", ") : "—"}</td><td>{status.platforms_without_login.length > 0 ? <span className="tag orange">⚠ 需登录</span> : <span className="tag green">OK</span>}</td></tr>
                  <tr><td>断点恢复 · 中断任务</td><td>{status.recovery.interrupted_tasks}</td><td><span className="tag gray">{status.recovery.interrupted_tasks === 0 ? "无" : "已恢复"}</span></td></tr>
                  <tr><td>断点恢复 · 待核实</td><td>{status.recovery.pending_verify}</td><td><span className="tag gray">{status.recovery.pending_verify === 0 ? "无" : "需人工确认"}</span></td></tr>
                </tbody>
              </table>
            </div>
          </div>
        )}

        {step === "goals" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>⑥ 阶段目标</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              R21 口径：每个目标必须同时定义 6 个字段（指标 + 目标值 + 口径 + 基线 + 周期 + 来源）。
            </p>
            {goals.length === 0 ? (
              <div className="note">还没有阶段目标 → 到 <strong>目标与策略</strong> 创建</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>目标</th><th>平台</th><th>指标</th><th>目标值</th><th>当前值</th><th>口径</th><th>达成率</th><th>状态</th></tr></thead>
                  <tbody>
                    {goals.map((g) => (
                      <tr key={g.id}>
                        <td style={{ maxWidth: 220 }}>{g.goal_text}</td>
                        <td>{g.target_platform ? PLATFORM_NAMES[g.target_platform] : "全局"}</td>
                        <td style={{ fontFamily: "monospace", fontSize: 11 }}>{g.target_metric}</td>
                        <td>{g.target_value}</td>
                        <td>{g.current_value !== null ? g.current_value : <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td style={{ fontSize: 11 }}>{g.metric_scope}</td>
                        <td>{g.achievement_rate !== null ? `${g.achievement_rate.toFixed(1)}%` : <span style={{ color: "var(--subtle)" }}>数据待采集</span>}</td>
                        <td><span className={`tag ${g.status === "active" ? "green" : "gray"}`}>{g.status}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {step === "check" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>⑦ 检查清单 · 启动首轮</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              以下全绿才能开始真实生产。点「启动首轮」→ 创建 spiral_round → RunRound 执行选题评分 + G → G1 → G2 终极目标检查 → H 图片卡片 → I 存草稿。
            </p>
            <div className="list">
              <CheckRow ok={checks.persona_ok}    label="账号画像"       detail={cfg.readonly.persona_keywords.join(", ")} />
              <CheckRow ok={checks.platforms_ok}   label="启用平台"       detail={`${status.enabled_platforms.length} 个 (${status.enabled_platforms.map((p) => PLATFORM_NAMES[p]).join(", ")})`} />
              <CheckRow ok={checks.login_ok}       label="登录态"         detail={`${status.logged_in_platforms.length} 个已登录`} />
              <CheckRow ok={checks.kb_ok}          label="选题库 KB"      detail={`${cfg.readonly.kb_type} @ ${cfg.readonly.kb_path}`} />
              <CheckRow ok={checks.llm_ok}         label="LLM API Key"    detail={cfg.readonly.llm_has_api_key ? "✅ 已配置" : "❌ 缺失"} />
              <CheckRow ok={checks.goals_ok}        label="阶段目标"       detail={`${goals.length} 条`} />
            </div>

            <div style={{ marginTop: 20, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className="btn primary"
                disabled={!allOk}
                onClick={() => {
                  // 跳转到 Production 页，那里有「创建首轮」按钮
                  const url = new URL(window.location.href);
                  url.searchParams.set("page", "production");
                  window.history.replaceState({}, "", url.toString());
                  window.dispatchEvent(new PopStateEvent("popstate"));
                }}
              >🚀 全部就绪 → 跳转到生产页启动首轮</button>
              {!allOk && <span style={{ color: "var(--orange)", fontSize: 12 }}>⚠ 还有项未通过，先完成配置</span>}
            </div>
          </div>
        )}
      </div>

      {/* 上一步 / 下一步 */}
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 16 }}>
        <button
          className="btn sm"
          disabled={stepIdx === 0}
          onClick={() => setStepIdx(stepIdx - 1)}
        >← 上一步</button>
        <div style={{ fontSize: 12, color: "var(--muted)", alignSelf: "center" }}>
          {stepIdx + 1} / {STEPS.length}
        </div>
        <button
          className="btn sm"
          disabled={stepIdx === STEPS.length - 1}
          onClick={() => setStepIdx(stepIdx + 1)}
        >下一步 →</button>
      </div>
    </>
  );
}

// ===== 子组件 =====
function FormRow({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>{label}</div>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        style={{
          width: "100%", padding: "8px 12px", background: "var(--card)",
          border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", fontSize: 13,
        }}
      />
    </div>
  );
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ padding: 12, background: "var(--card)", borderRadius: 8, border: "1px solid var(--border)" }}>
      <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 13, fontFamily: mono ? "monospace" : undefined, color: "var(--text)" }}>{value}</div>
    </div>
  );
}

function CheckRow({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className="task-row">
      <i className={`task-prio ${ok ? "green" : "orange"}`} />
      <div className="task-main">
        <div className="task-title">{label}</div>
        <div className="task-desc" style={{ fontFamily: "monospace", fontSize: 11 }}>{detail}</div>
      </div>
      <div>{ok ? <span className="tag green">✅ 通过</span> : <span className="tag orange">❌ 缺失</span>}</div>
    </div>
  );
}
