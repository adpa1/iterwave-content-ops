import { useEffect, useState } from "react";
import { api, Platform, SystemConfigView, SystemStatus, Goal, DiagnoseResult } from "../api";
import { navigate } from "../nav";
import { PersonaEditor, KbEditor, LlmEditor, PlatformToggles } from "../components/ConfigEditors";
import { useApp } from "../AppContext";

const STEPS = [
  { key: "persona",  label: "① 账号画像",     hint: "做什么内容、不做什么" },
  { key: "platforms", label: "② 启用平台",    hint: "建议先用 1 个平台跑通" },
  { key: "kb",       label: "③ 选题库",    hint: "上传 / 远程地址 / 服务器路径，可跳过" },
  { key: "llm",      label: "④ 模型配置",     hint: "模型和 API Key" },
  { key: "skills",   label: "⑤ 运行环境",  hint: "登录与运行状态" },
  { key: "goals",    label: "⑥ 阶段目标",     hint: "可先跳过，试跑后再定" },
  { key: "check",    label: "⑦ 检查清单",     hint: "检查并开始第一轮" },
] as const;

type StepKey = typeof STEPS[number]["key"];

const PLATFORM_NAMES: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
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
  { value: "account_stats", label: "account_stats 平台读回" },
  { value: "platform_api", label: "平台 API" },
  { value: "manual", label: "人工录入" },
];

function todayStr(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

export default function Onboarding() {
  const { showToast } = useApp();
  const [stepIdx, setStepIdx] = useState(0);
  const [cfg, setCfg] = useState<SystemConfigView | null>(null);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [goals, setGoals] = useState<Goal[]>([]);
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
      showToast("✓ 阶段目标已创建", "success");
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
            7 步完成 IterWave 初始化。每一步都能直接编辑并保存，保存后立即生效。
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
              上传一份选题表（只要有「标题」列就行），或填远程地址 / 服务器路径。暂时没有也可以跳过，用热点转入和手动添加。
            </p>
            <KbEditor view={cfg} onSaved={reload} />
          </div>
        )}

        {step === "llm" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>④ 模型配置</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              选择服务商与模型并填写 API Key（Key 只保存在本机，不会回显）。
            </p>
            <LlmEditor view={cfg} onSaved={reload} />
            {!cfg.readonly.llm_has_api_key && (
              <div className="note" style={{ marginTop: 12, color: "var(--orange)" }}>
                ! 还没填 API Key：文案会用固定模板生成，热点匹配只按关键词判断，复盘建议也不会写进下一轮文案。
              </div>
            )}
          </div>
        )}

        {step === "skills" && (
          <div>
            <div className="panel-title" style={{ marginBottom: 16 }}>⑤ 运行环境</div>
            <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 14 }}>
              当前系统状态：平台登录、是否被限流暂停、正在运行的轮次。
            </p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>项目</th><th>当前</th><th>状态</th></tr></thead>
                <tbody>
                  <tr><td>评分模式</td><td>{({ cold_start: "冷启动", sample_accumulating: "样本积累", stable: "稳定运行", stable_operation: "稳定运行" } as Record<string, string>)[status.scoring_mode] ?? status.scoring_mode}</td><td><span className="tag green">✓</span></td></tr>
                  <tr><td>活跃轮次</td><td>{status.active_rounds}</td><td><span className={`tag ${status.active_rounds > 0 ? "blue" : "gray"}`}>{status.active_rounds > 0 ? "运行中" : "空闲（正常）"}</span></td></tr>
                  <tr><td>已发布样本</td><td>{status.published_samples}</td><td><span className={`tag ${status.published_samples >= 10 ? "green" : "orange"}`}>{status.published_samples >= 10 ? "样本充足" : "冷启动中"}</span></td></tr>
                  <tr><td>暂停中的平台</td><td>{status.circuit_broken_platforms.length > 0 ? status.circuit_broken_platforms.join(", ") : "—"}</td><td>{status.circuit_broken_platforms.length > 0 ? <span className="tag red">! 阻塞</span> : <span className="tag green">OK</span>}</td></tr>
                  <tr><td>缺登录态平台</td><td>{status.platforms_without_login.length > 0 ? status.platforms_without_login.map((p) => PLATFORM_NAMES[p]).join(", ") : "—"}</td><td>{status.platforms_without_login.length > 0 ? <span className="tag orange">! 需登录</span> : <span className="tag green">OK</span>}</td></tr>
                  <tr><td>重启时接续的任务</td><td>{status.recovery.interrupted_tasks}</td><td><span className="tag gray">{status.recovery.interrupted_tasks === 0 ? "无" : "已恢复"}</span></td></tr>
                  <tr><td>结果待确认的操作</td><td>{status.recovery.pending_verify}</td><td><span className="tag gray">{status.recovery.pending_verify === 0 ? "无" : "需人工确认"}</span></td></tr>
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
              红色项必须先处理；黄色项不影响开始，但会影响效果。开始后系统会：挑选题 → 按各平台风格写文案 → 质量检查 → 出图 → 存到各平台草稿箱，等你去平台点发布。
            </p>
            {diag && (
              <div className="table-wrap" style={{ marginBottom: 14 }}>
                <table>
                  <thead><tr><th style={{ width: 36 }}>#</th><th>检查项</th><th style={{ width: 50 }}>结果</th><th>说明</th><th style={{ width: 86 }}></th></tr></thead>
                  <tbody>
                    {diag.items.map((it) => (
                      <tr key={it.no}>
                        <td>{it.no}</td><td>{it.name}</td>
                        <td>{it.status === "pass"
                          ? <span className="tag green">通过</span>
                          : it.status === "warn" ? <span className="tag orange">建议</span> : <span className="tag red">必须</span>}</td>
                        <td style={{ fontSize: 12 }}>{it.message}</td>
                        <td>{it.fix_page && it.status !== "pass" && (
                          <button className="btn sm" onClick={() => navigate(it.fix_page!, it.fix_tab ?? undefined)}>去处理</button>
                        )}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <button className="btn sm" onClick={runDiag} disabled={diagBusy} style={{ marginBottom: 12 }}>
              {diagBusy ? "检查中…" : "↻ 重新检查"}
            </button>
            {goals.length === 0 && (
              <div className="note" style={{ marginBottom: 12 }}>
                还没有设置目标也可以先试跑一轮：看看生成的内容是否符合预期，有了第一批数据再定目标更准确。
                <button className="btn sm ghost" onClick={() => navigate("goals")}>去设置目标</button>
              </div>
            )}

            <div style={{ marginTop: 8, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className="btn primary"
                disabled={!diag || diag.overall === "block"}
                onClick={() => navigate("production", "pipeline")}
              >▶ {goals.length ? "开始第一轮" : "试跑第一轮"}</button>
              {diag && diag.overall === "block" && (
                <span style={{ color: "var(--orange)", fontSize: 12 }}>
                  先处理标红的项：{diag.items.filter((i) => i.status === "block").map((i) => i.name).join("、")}
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
