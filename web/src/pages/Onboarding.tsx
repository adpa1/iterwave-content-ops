import { useEffect, useState } from "react";
import { api, Platform, SystemConfigView, SystemStatus, Goal, DiagnoseResult } from "../api";
import { navigate } from "../nav";
import { PersonaEditor, KbEditor, LlmEditor, PlatformToggles } from "../components/ConfigEditors";
import { useApp } from "../AppContext";

const STEPS = [
  { key: "persona",  label: "① 账号画像",     hint: "终极目标 / 红线 / 定位关键词" },
  { key: "platforms", label: "② 启用平台",    hint: "至少启用 1 个；建议先跑通 1 个平台" },
  { key: "kb",       label: "③ 选题库",       hint: "上传或连接知识库来源" },
  { key: "llm",      label: "④ 模型配置",     hint: "模型服务与密钥" },
  { key: "skills",   label: "⑤ 运行环境",     hint: "检查登录与运行环境" },
  { key: "goals",    label: "⑥ 阶段目标",     hint: "指标、目标值与基线" },
  { key: "check",    label: "⑦ 检查清单",     hint: "预检 + 启动首轮" },
] as const;

type StepKey = typeof STEPS[number]["key"];

const PLATFORM_NAMES: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
};

const SCORING_MODE_LABELS: Record<string, string> = {
  cold_start: "冷启动（已发布样本 < 10）",
  sample_accumulating: "样本积累（部分维度样本 < 5）",
  stable: "稳定运行（活跃维度样本 ≥ 5）",
};

const GOAL_METRICS = [
  { value: "followers_gained", label: "新增粉丝" },
  { value: "likes", label: "点赞数" },
  { value: "collects", label: "收藏数" },
  { value: "views", label: "曝光数" },
  { value: "comments", label: "评论数" },
  { value: "shares", label: "分享数" },
  { value: "interaction_rate", label: "互动率" },
];

const GOAL_DATA_SOURCES = [
  { value: "account_stats", label: "平台数据读回" },
  { value: "platform_api", label: "平台 API" },
  { value: "manual", label: "人工录入" },
];

function todayStr(offsetDays = 0): string {
  // 使用本地日历日期，避免 UTC 时区换算导致目标起止日期偏移一天。
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export default function Onboarding() {
  const { showToast } = useApp();
  const [stepIdx, setStepIdx] = useState(0);
  const [cfg, setCfg] = useState<SystemConfigView | null>(null);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [availableTopicCount, setAvailableTopicCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // ⑥ 阶段目标：向导内直接创建（POST /api/goals → goals 表）
  const [goalForm, setGoalForm] = useState({
    goal_text: "",
    target_platform: "",
    target_metric: "followers_gained",
    target_value: "500",
    metric_scope: "delta",
    baseline_value: "0",
    baseline_at: todayStr(),
    metric_period_start: todayStr(),
    metric_period_end: todayStr(30),
    data_source: "account_stats",
  });
  const [goalBusy, setGoalBusy] = useState(false);

  const step: StepKey = STEPS[stepIdx].key;
  const [diag, setDiag] = useState<DiagnoseResult | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const runDiag = async () => {
    setDiagBusy(true);
    try { setDiag(await api.diagnose(false)); } catch { /* 诊断失败时仍显示本地预检 */ }
    finally { setDiagBusy(false); }
  };
  useEffect(() => { if (step === "check" && !diag) runDiag(); }, [step]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 配置 / 状态 / 目标统一重载（各步编辑器保存后调用） */
  const reload = async () => {
    try {
      const [c, s, g] = await Promise.all([api.config(), api.status(), api.goals()]);
      setCfg(c);
      setStatus(s);
      setGoals(g);
      try {
        const pool = await api.topicPool({ status: "active", limit: 1 });
        setAvailableTopicCount(pool.total);
      } catch {
        setAvailableTopicCount(0);
      }
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
    }
  };

  useEffect(() => {
    (async () => {
      setLoading(true);
      await reload();
      setLoading(false);
    })();
  }, []);

  const createGoal = async () => {
    if (!goalForm.goal_text.trim()) { showToast("请填写目标描述", "error"); return; }
    const targetValue = Number(goalForm.target_value);
    if (!(targetValue > 0)) { showToast("目标值必须大于 0", "error"); return; }
    setGoalBusy(true);
    try {
      await api.createGoal({
        goal_text: goalForm.goal_text.trim(),
        target_metric: goalForm.target_metric,
        target_value: targetValue,
        target_platform: (goalForm.target_platform || null) as Platform | null,
        metric_scope: goalForm.metric_scope,
        baseline_value: Number(goalForm.baseline_value) || 0,
        baseline_at: goalForm.baseline_at,
        metric_period_start: goalForm.metric_period_start,
        metric_period_end: goalForm.metric_period_end,
        data_source: goalForm.data_source,
      });
      showToast("✓ 阶段目标已创建并写入 goals 表", "success");
      await reload();
      setGoalForm((f) => ({ ...f, goal_text: "" }));
    } catch (e: any) {
      showToast(`创建失败: ${e?.message ?? e}`, "error");
    } finally {
      setGoalBusy(false);
    }
  };

  if (loading) return <div className="note">加载配置中…</div>;
  if (err) return <div className="note" style={{ color: "var(--red)" }}>加载失败：{err}</div>;
  if (!cfg || !status) return null;

  // 预检项
  const checks = {
    persona_ok: cfg.readonly.persona_configured && cfg.readonly.persona_keywords.length > 0,
    platforms_ok: status.enabled_platforms.length >= 1,
    kb_ok: availableTopicCount !== null && availableTopicCount > 0,
    llm_ok: cfg.readonly.llm_has_api_key,
    login_ok: status.logged_in_platforms.length >= 1,
    goals_ok: goals.length >= 1,
  };
  /** 阶段目标不阻塞开跑：没有目标也能试跑第一轮，只是不统计达成率 */
  const blockingOk = checks.persona_ok && checks.platforms_ok && checks.kb_ok
    && checks.llm_ok && checks.login_ok;
  const blocked = diag ? diag.overall === "block" : !blockingOk;
  // 进度只表示当前配置确实满足要求，不表示用户曾经点过“下一步”。
  const stepPassed: Record<StepKey, boolean> = {
    persona: checks.persona_ok,
    platforms: checks.platforms_ok,
    kb: checks.kb_ok,
    llm: checks.llm_ok,
    skills: checks.login_ok,
    goals: checks.goals_ok,
    check: !!diag && diag.overall !== "block" && blockingOk,
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">首次配置</div>
          <h1 className="page-title">首次配置向导</h1>
          <div className="page-desc">
            7 步完成初始化。每一步都能直接编辑并保存，保存后立即生效。
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
                background: i === stepIdx ? "var(--blue)" : stepPassed[s.key] ? "rgba(66,99,235,0.1)" : "var(--card)",
                color: i === stepIdx ? "#fff" : stepPassed[s.key] ? "var(--blue)" : "var(--muted)",
                border: `1px solid ${i === stepIdx ? "var(--blue)" : "var(--border)"}`,
                fontSize: 12,
                transition: "all .15s",
              }}
            >
              <div style={{ fontWeight: 700 }}>{s.label}</div>
              <div style={{ fontSize: 10, opacity: 0.8, marginTop: 2 }}>{s.hint}</div>
              {stepPassed[s.key] && <div style={{ fontSize: 16, marginTop: 4 }}>✓ 已通过</div>}
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
              账号定位关键词 + 禁区。这些决定了 终极目标检查器拦截什么内容。
            </p>
            <PersonaEditor view={cfg} onSaved={reload} />
          </div>
        )}

        {step === "platforms" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>② 启用平台</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              至少启用 1 个平台才能跑通首轮。建议先 1 平台验证，再扩。
            </p>
            <PlatformToggles view={cfg} onSaved={reload} loggedIn={status.logged_in_platforms} />
            <div className="note" style={{ marginTop: 10 }}>
              当前启用 {status.enabled_platforms.length} / 4 个平台：{status.enabled_platforms.map((p) => PLATFORM_NAMES[p]).join("、") || "尚未启用"}。
              未启用的平台不会参与本轮生产；账号页展示的是全部支持平台，不代表全部已启用。
            </div>
            {status.circuit_broken_platforms.length > 0 && (
              <div className="note" style={{ marginTop: 12, color: "var(--orange)" }}>
                ! 已熔断：{status.circuit_broken_platforms.map((p) => PLATFORM_NAMES[p]).join("、")}
                （需在「运营控制台」走 3 步恢复验证）
              </div>
            )}
            <div className="note" style={{ marginTop: 12 }}>
              ▣ 启用 ≠ 可发布：还需要扫码登录，入口在「账号与登录」页。
            </div>
          </div>
        )}

        {step === "kb" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>③ 选题库</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              选题来源可以是本地上传（CSV / JSON / JSONL / XLSX）、远程地址（含 RSS），也可以使用管理员添加的服务器来源。
              来源至少要有「标题」和「维度」两列。完整来源管理在「内容资产 → 知识库来源」。
            </p>
            <KbEditor view={cfg} onSaved={reload} />
          </div>
        )}

        {step === "llm" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>④ 模型配置</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              选择模型服务商与模型，并填写服务密钥（密钥只保存在服务端，不会回显）。
            </p>
            <LlmEditor view={cfg} onSaved={reload} />
            {!cfg.readonly.llm_has_api_key && (
              <div className="note" style={{ marginTop: 12, color: "var(--orange)" }}>
                ! 服务密钥尚未配置，选题评分和文案生成将无法调用模型。
              </div>
            )}
          </div>
        )}

        {step === "skills" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>⑤ 运行环境预检</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              核心运行依赖一次性检查：登录态 / 熔断 / 评分模式 / 活跃轮次 / 断点恢复。
            </p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>信号</th><th>值</th><th>状态</th></tr></thead>
                <tbody>
                  <tr><td>评分模式</td><td>{SCORING_MODE_LABELS[status.scoring_mode] ?? status.scoring_mode}</td><td><span className="tag green">✓</span></td></tr>
                  <tr><td>活跃轮次</td><td>{status.active_rounds}</td><td><span className={`tag ${status.active_rounds > 0 ? "blue" : "gray"}`}>{status.active_rounds > 0 ? "运行中" : "空闲（正常）"}</span></td></tr>
                  <tr><td>已发布样本</td><td>{status.published_samples}</td><td><span className={`tag ${status.published_samples >= 10 ? "green" : "orange"}`}>{status.published_samples >= 10 ? "样本充足" : "冷启动中"}</span></td></tr>
                  <tr><td>熔断平台</td><td>{status.circuit_broken_platforms.length > 0 ? status.circuit_broken_platforms.join(", ") : "—"}</td><td>{status.circuit_broken_platforms.length > 0 ? <span className="tag red">! 阻塞</span> : <span className="tag green">OK</span>}</td></tr>
                  <tr><td>缺登录态平台</td><td>{status.platforms_without_login.length > 0 ? status.platforms_without_login.map((p) => PLATFORM_NAMES[p]).join(", ") : "—"}</td><td>{status.platforms_without_login.length > 0 ? <span className="tag orange">! 需登录</span> : <span className="tag green">OK</span>}</td></tr>
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
              每个目标必须同时定义 6 个字段（指标 + 目标值 + 口径 + 基线 + 周期 + 来源）。
            </p>

            <div style={{ display: "grid", gap: 12, padding: 14, border: "1px solid var(--border)", borderRadius: 10, marginBottom: 16 }}>
              <FormRow label="目标描述 *" value={goalForm.goal_text}
                onChange={(v) => setGoalForm({ ...goalForm, goal_text: v })}
                placeholder="如：小红书 30 天新增 500 粉" />
              <div className="grid three">
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>平台</div>
                  <select className="field" style={{ width: "100%" }} value={goalForm.target_platform}
                    onChange={(e) => setGoalForm({ ...goalForm, target_platform: e.target.value })}>
                    <option value="">全平台</option>
                    {(Object.keys(PLATFORM_NAMES) as Platform[]).map((p) => (
                      <option key={p} value={p}>{PLATFORM_NAMES[p]}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>指标</div>
                  <select className="field" style={{ width: "100%" }} value={goalForm.target_metric}
                    onChange={(e) => setGoalForm({ ...goalForm, target_metric: e.target.value })}>
                    {GOAL_METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                  </select>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>目标值 *</div>
                  <input className="field" type="number" style={{ width: "100%" }} value={goalForm.target_value}
                    onChange={(e) => setGoalForm({ ...goalForm, target_value: e.target.value })} />
                </div>
              </div>
              <div className="grid three">
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>口径</div>
                  <select className="field" style={{ width: "100%" }} value={goalForm.metric_scope}
                    onChange={(e) => setGoalForm({ ...goalForm, metric_scope: e.target.value })}>
                    <option value="delta">增量（本阶段新增）</option>
                    <option value="total">总量（达到总数）</option>
                  </select>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>基线值</div>
                  <input className="field" type="number" style={{ width: "100%" }} value={goalForm.baseline_value}
                    onChange={(e) => setGoalForm({ ...goalForm, baseline_value: e.target.value })} />
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>数据来源</div>
                  <select className="field" style={{ width: "100%" }} value={goalForm.data_source}
                    onChange={(e) => setGoalForm({ ...goalForm, data_source: e.target.value })}>
                    {GOAL_DATA_SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid three">
                <FormRow label="基线时间" value={goalForm.baseline_at} onChange={(v) => setGoalForm({ ...goalForm, baseline_at: v })} />
                <FormRow label="周期开始" value={goalForm.metric_period_start} onChange={(v) => setGoalForm({ ...goalForm, metric_period_start: v })} />
                <FormRow label="周期结束" value={goalForm.metric_period_end} onChange={(v) => setGoalForm({ ...goalForm, metric_period_end: v })} />
              </div>
              <button className="btn primary sm" style={{ justifySelf: "start" }} disabled={goalBusy}
                onClick={createGoal}>{goalBusy ? "创建中…" : "＋ 创建阶段目标"}</button>
            </div>

            {goals.length === 0 ? (
              <div className="note">还没有阶段目标，用上方表单创建第一条。</div>
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
              以下必需项全部通过才能开始真实生产。首轮依次完成：选题评分 → 文案生成 → 终极目标检查 → 图片卡片 → 保存草稿（不会自动发布）。
            </p>
            {diag && (
              <div className="table-wrap" style={{ marginBottom: 14 }}>
                <table>
                  <thead><tr><th style={{ width: 36 }}>#</th><th>自检清单（{diag.items.length} 项）</th><th style={{ width: 50 }}>结果</th><th>说明</th><th style={{ width: 86 }}></th></tr></thead>
                  <tbody>
                    {diag.items.map((it) => (
                      <tr key={it.no}>
                        <td>{it.no}</td><td>{it.name}</td>
                        <td>{it.status === "pass" ? "✓" : it.status === "warn" ? "●" : "●"}</td>
                        <td style={{ fontSize: 12 }}>{it.message}</td>
                        <td>{it.fix_page && it.status !== "pass" && (
                          <button className="btn sm" onClick={() => navigate(it.fix_page!, it.fix_tab ?? undefined)}>去修复</button>
                        )}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <button className="btn sm" onClick={runDiag} disabled={diagBusy} style={{ marginBottom: 12 }}>
              {diagBusy ? "诊断中…" : "↻ 重新诊断"}
            </button>
            <div className="list">
              <CheckRow ok={checks.persona_ok}    label="账号画像"       detail={checks.persona_ok ? cfg.readonly.persona_keywords.join("、") : "还没设置账号定位，请填写定位关键词并保存"} />
              <CheckRow ok={checks.platforms_ok}   label="启用平台"       detail={`${status.enabled_platforms.length} 个 (${status.enabled_platforms.map((p) => PLATFORM_NAMES[p]).join(", ")})`} />
              <CheckRow ok={checks.login_ok}       label="登录态"         detail={`${status.logged_in_platforms.length} 个已登录`} />
              <CheckRow ok={checks.kb_ok}          label="选题库"      detail={availableTopicCount === null ? "正在检查选题库" : availableTopicCount > 0 ? `可用选题 ${availableTopicCount} 条` : "没有可用选题，请添加或同步知识库来源"} />
              <CheckRow ok={checks.llm_ok}         label="模型服务密钥"    detail={cfg.readonly.llm_has_api_key ? "✓ 已配置" : "✕ 缺失"} />
              <CheckRow ok={checks.goals_ok}        label="阶段目标"       detail={`${goals.length} 条`} />
            </div>

            <div style={{ marginTop: 20, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className="btn primary"
                disabled={blocked}
                onClick={() => navigate("production", "pipeline")}
              >{checks.goals_ok ? "↗ 开始第一轮" : "↗ 试跑第一轮（暂不设阶段目标）"}</button>
              {blocked
                ? <span style={{ color: "var(--orange)", fontSize: 12 }}>! 还有阻塞项未通过，先完成配置</span>
                : !checks.goals_ok && (
                  <span style={{ color: "var(--muted)", fontSize: 12 }}>
                    没设阶段目标也能试跑：本轮照常产出草稿，只是暂时不统计目标达成率
                  </span>
                )}
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
          onClick={() => { setStepIdx(stepIdx + 1); }}
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

function CheckRow({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className="task-row">
      <i className={`task-prio ${ok ? "green" : "orange"}`} />
      <div className="task-main">
        <div className="task-title">{label}</div>
        <div className="task-desc" style={{ fontFamily: "monospace", fontSize: 11 }}>{detail}</div>
      </div>
      <div>{ok ? <span className="tag green">✓ 通过</span> : <span className="tag orange">✕ 缺失</span>}</div>
    </div>
  );
}
