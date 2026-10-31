import { useEffect, useMemo, useState } from "react";
import EmptyHint from "../components/EmptyHint";
import { useApp } from "../AppContext";
import { navigate, useUrlTab } from "../nav";
import {
  api, Platform, SystemStatus, AiCallRecord, UserOverrideRecord, RedlineRecord,
  OrchestrationView, OrchestrationRun, DiagnoseResult, StepState, TenantSkillManifest,
} from "../api";

const PLATFORM_LABEL: Record<string, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
};

const TABS = [
  { key: "preview", label: "编排预览" },
  { key: "edit", label: "编辑编排" },
  { key: "diagnose", label: "流程诊断" },
  { key: "trace", label: "平台运行追踪" },
  { key: "skills", label: "Skills 注册表" },
  { key: "audit", label: "AI 审计链" },
] as const;
type TabKey = typeof TABS[number]["key"];

const STATE_META: Record<StepState | "off", { cls: string; text: string }> = {
  success: { cls: "green", text: "成功" },
  failed: { cls: "red", text: "失败" },
  skipped: { cls: "gray", text: "跳过" },
  pending: { cls: "blue", text: "待执行" },
  off: { cls: "gray", text: "已关闭" },
};

const ROUND_STATUS_LABEL: Record<string, string> = {
  created: "已创建", producing: "生产中", awaiting_publish: "待发布", collecting_metrics: "采集中",
  analyzing: "复盘中", completed: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已取消",
};

const SKILLS = [
  // 按「运营工作流」分组
  { group: "内容生产", rows: [
    { id: "topic_scoring",      name: "选题评分引擎",        src: "src/core/scoring/",                 status: "OK", note: "三模式自动切换" },
    { id: "prompt_variants",    name: "四平台变体生成",      src: "src/core/content_pipeline.py",       status: "OK", note: "同选题 × 四风格" },
    { id: "ultimate_goal_check",name: "终极目标检查（R2）", src: "src/core/guard/",                    status: "OK", note: "三入口 check_content/strategy/comment" },
    { id: "image_card",         name: "图片卡片制作",        src: "src/core/content_pipeline.py",        status: "OK", note: "Pillow + OpenCV" },
    { id: "draft_saver",        name: "Playwright 存草稿",  src: "src/adapters/",       status: "!", note: "R1：只点存草稿" },
  ]},
  { group: "运营与复盘", rows: [
    { id: "circuit_breaker",    name: "熔断器（R6）",       src: "src/core/ops/circuit_breaker.py",   status: "OK", note: "5 信号 + 3 步恢复" },
    { id: "rhythm_controller",  name: "节奏控制器",          src: "src/core/ops/rhythm.py",            status: "OK", note: "平台独立配额 + 时段窗口" },
    { id: "ops_engine",         name: "运营引擎",           src: "src/core/ops/engine.py",            status: "!", note: "被动回复已接；主动评论/关注极低配额" },
    { id: "spiral_engine",      name: "螺旋引擎 + 缺口分析", src: "src/core/spiral/",                  status: "OK", note: "三通道输出" },
    { id: "zscore_normalize",   name: "跨平台归一化（R13）", src: "src/core/metrics/",                 status: "OK", note: "平台内 z-score" },
  ]},
  { group: "支撑层", rows: [
    { id: "recovery_manager",  name: "断点恢复（R16）",    src: "src/core/recovery.py",              status: "OK", note: "任务记录持久化 + 幂等覆盖" },
    { id: "autogrow",           name: "自动闭环（自生长）",   src: "src/core/autogrow.py",              status: "OK", note: "复盘→评分/文案，条件满足自动开下一轮" },
    { id: "topic_pool",         name: "选题池",              src: "src/core/topic_pool.py",            status: "OK", note: "本地库 + 热点 + 手动，软删除可恢复" },
    { id: "orchestration",      name: "编排与诊断",           src: "src/core/orchestration.py",         status: "OK", note: "步骤开关 / 依赖校验 / 自检清单" },
    { id: "scheduler",          name: "定时任务调度（11 类）", src: "src/core/scheduler/",             status: "OK", note: "任务记录持久化到本地数据库" },
    { id: "platform_readback",  name: "平台数据读回",        src: "src/vendor/easel/",                 status: "!", note: "account_stats / platform_readback 需登录态" },
  ]},
];

export default function Agent() {
  const [tab, setTab] = useUrlTab<TabKey>(TABS.map((t) => t.key), "preview");
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [orch, setOrch] = useState<OrchestrationView | null>(null);
  const [runs, setRuns] = useState<OrchestrationRun[] | null>(null);

  const load = async () => {
    setError(null);
    try {
      const [s, o, r] = await Promise.all([api.status(), api.orchestration(), api.orchestrationRuns(10)]);
      setStatus(s); setOrch(o); setRuns(r.runs);
    } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">智能体与流程</div>
          <h1 className="page-title">Agent 编排与诊断</h1>
          <div className="page-desc">
            选题 → 文案 → 质量门 → 出图 → 存草稿 → 排期 → 等待发布。编排保存后下一轮立即按新编排执行。
          </div>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={() => setTab("diagnose")}>⚑ 流程诊断</button>
          <button className="btn" onClick={load}>↻ 刷新</button>
        </div>
      </div>

      <div className="tabs" role="tablist" style={{ marginBottom: 14 }}>
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key}
            className={`tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="panel" style={{ borderColor: "var(--red)", marginBottom: 14 }}>
          <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>! 加载失败：{error}</span>
            <button className="btn sm" onClick={load}>↻ 重试</button>
          </div>
        </div>
      )}

      {tab === "preview" && <PreviewPane orch={orch} runs={runs} onEdit={() => setTab("edit")} />}
      {tab === "edit" && <EditPane orch={orch} onSaved={(v) => { setOrch(v); }} />}
      {tab === "diagnose" && <DiagnosePane />}
      {tab === "trace" && <TracePane status={status} runs={runs} />}
      {tab === "skills" && <SkillsPane />}
      {tab === "audit" && <AuditPane />}
    </>
  );
}

// ============================================================ 编排预览
function PreviewPane({ orch, runs, onEdit }: {
  orch: OrchestrationView | null; runs: OrchestrationRun[] | null; onEdit: () => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [runIdx, setRunIdx] = useState(0);
  if (!orch || !runs) return <div className="note">加载中…</div>;
  const run = runs[runIdx];
  const labelOf = (id: string) => orch.steps.find((s) => s.id === id)?.label ?? id;

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">⎇</span>当前编排</div>
          <div className="panel-sub">点步骤查看依赖、输入与最近一轮执行情况</div>
        </div>
        <div className="flex" style={{ gap: 8, alignItems: "center" }}>
          {runs.length > 0 && (
            <>
              <label htmlFor="run-pick" className="sr-only">选择轮次</label>
              <select id="run-pick" className="field" value={runIdx} onChange={(e) => setRunIdx(Number(e.target.value))}>
                {runs.map((r, i) => (
                  <option key={r.round_id} value={i}>
                    第 {r.round_number} 轮 · {ROUND_STATUS_LABEL[r.status] ?? r.status}{r.trigger_source === "auto" ? " · 自动" : ""}
                  </option>
                ))}
              </select>
            </>
          )}
          <button className="btn sm" onClick={onEdit}>✎ 编辑编排</button>
        </div>
      </div>

      <div className="agent-flow" style={{ gridTemplateColumns: `repeat(${orch.steps.length}, minmax(0, 1fr))` }}>
        {orch.steps.map((s, i) => {
          const st = s.enabled ? (run?.step_status?.[s.id]?.status ?? null) : "off";
          const cls = st === "success" ? "done" : st === "failed" ? "pause" : openId === s.id ? "active" : "";
          return (
            <button key={s.id} type="button" className={`flow-step ${cls}`}
              style={{ textAlign: "left", cursor: "pointer", opacity: s.enabled ? 1 : 0.5, font: "inherit" }}
              aria-expanded={openId === s.id} onClick={() => setOpenId(openId === s.id ? null : s.id)}>
              <div className="num">STEP 0{i + 1}{s.locked ? " · 锁定" : ""}</div>
              <div className="ft">{s.label.replace(/^[①-⑦]\s*/, "")}</div>
              <div className="fs">
                {st ? <span className={`tag ${STATE_META[st as StepState | "off"].cls}`}>{STATE_META[st as StepState | "off"].text}</span>
                    : <span className="tag gray">无记录</span>}
              </div>
            </button>
          );
        })}
      </div>

      {openId && (() => {
        const s = orch.steps.find((x) => x.id === openId)!;
        const st = run?.step_status?.[s.id];
        return (
          <div className="note" style={{ marginTop: 12, padding: 14, background: "var(--surface-2)", borderRadius: 10 }}>
            <div style={{ fontWeight: 700, marginBottom: 6 }}>{s.label} <code style={{ fontSize: 11, color: "var(--muted)" }}>skill: {s.skill}</code></div>
            <div style={{ marginBottom: 6 }}>{s.desc}</div>
            <div className="kv-grid">
              <span>依赖</span><span>{s.requires.length ? s.requires.map(labelOf).join("、") : "无"}</span>
              <span>输入</span><span>{s.inputs.join("、")}</span>
              <span>状态</span><span>{s.enabled ? (s.locked ? "开启（锁定，不可关闭）" : "开启") : "已关闭"}</span>
              <span>最近执行</span><span>{st ? `${STATE_META[st.status].text}${st.detail ? ` · ${st.detail}` : ""}` : run ? "该轮无此步骤记录" : "还没有执行记录"}</span>
              {run?.duration_seconds != null && (<><span>整轮耗时</span><span>{run.duration_seconds} 秒</span></>)}
            </div>
          </div>
        );
      })()}

      {runs.length === 0 && (
        <EmptyHint next="到「内容生产」创建并运行一轮，这里会显示每一步的执行结果">还没有执行记录</EmptyHint>
      )}
      {run && run.errors.length > 0 && (
        <div className="note" style={{ marginTop: 10, color: "var(--orange)" }}>
          本轮提示：{run.errors.join("；")}
        </div>
      )}
    </div>
  );
}

// ============================================================ 编辑编排
function EditPane({ orch, onSaved }: { orch: OrchestrationView | null; onSaved: (v: OrchestrationView) => void }) {
  const { showToast, withPending, isPending } = useApp();
  const [steps, setSteps] = useState<{ id: string; enabled: boolean }[]>([]);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => { if (orch) setSteps(orch.steps.map((s) => ({ id: s.id, enabled: s.enabled }))); }, [orch]);

  const meta = useMemo(() => Object.fromEntries((orch?.steps ?? []).map((s) => [s.id, s])), [orch]);

  useEffect(() => {
    if (!steps.length) return;
    const t = setTimeout(() => {
      api.validateOrchestration(steps).then((r) => setErrors(r.errors)).catch(() => {});
    }, 150);
    return () => clearTimeout(t);
  }, [steps]);

  if (!orch) return <div className="note">加载中…</div>;

  const dirty = JSON.stringify(steps) !== JSON.stringify(orch.steps.map((s) => ({ id: s.id, enabled: s.enabled })));
  const move = (i: number, d: -1 | 1) => setSteps((prev) => {
    const j = i + d;
    if (j < 0 || j >= prev.length) return prev;
    const next = [...prev];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });
  const toggle = (id: string) => setSteps((prev) => prev.map((s) => s.id === id ? { ...s, enabled: !s.enabled } : s));

  const save = () => withPending("orch:save", async () => {
    try {
      const v = await api.saveOrchestration(steps);
      onSaved(v);
      showToast("✓ 编排已保存，下一轮按新编排执行", "success");
    } catch (e: any) { showToast(`保存失败：${e.message}`, "error"); }
  });
  const reset = () => withPending("orch:reset", async () => {
    try { onSaved(await api.resetOrchestration()); showToast("已恢复默认编排"); }
    catch (e: any) { showToast(`恢复失败：${e.message}`, "error"); }
  });

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">✎</span>编辑编排</div>
          <div className="panel-sub">开关步骤、调整顺序；保存前自动校验依赖（锁定步骤不能关闭）</div>
        </div>
        <div className="flex" style={{ gap: 8 }}>
          <button className="btn sm" onClick={reset} disabled={isPending("orch:reset")}>恢复默认</button>
          <button className="btn sm primary" onClick={save} disabled={!dirty || errors.length > 0 || isPending("orch:save")}>
            {isPending("orch:save") ? "保存中…" : "保存编排"}
          </button>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead><tr><th style={{ width: 56 }}>顺序</th><th>步骤</th><th>依赖</th><th style={{ width: 90 }}>启用</th><th style={{ width: 96 }}>调整</th></tr></thead>
          <tbody>
            {steps.map((s, i) => {
              const m = meta[s.id];
              return (
                <tr key={s.id} style={{ opacity: s.enabled ? 1 : 0.55 }}>
                  <td>{i + 1}</td>
                  <td>
                    <div style={{ fontWeight: 650 }}>{m?.label}</div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>{m?.desc}</div>
                  </td>
                  <td style={{ fontSize: 12 }}>{m?.requires.length ? m.requires.map((r: string) => meta[r]?.label ?? r).join("、") : "—"}</td>
                  <td>
                    <label className="flex" style={{ gap: 6, alignItems: "center" }}>
                      <input type="checkbox" checked={s.enabled} disabled={m?.locked} onChange={() => toggle(s.id)} />
                      <span style={{ fontSize: 12 }}>{m?.locked ? "锁定" : s.enabled ? "开" : "关"}</span>
                    </label>
                  </td>
                  <td>
                    <button className="btn sm" aria-label="上移" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>{" "}
                    <button className="btn sm" aria-label="下移" disabled={i === steps.length - 1} onClick={() => move(i, 1)}>↓</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="note" role="status" style={{ marginTop: 10, color: errors.length ? "var(--red)" : "var(--green)" }}>
        {errors.length ? errors.map((e, i) => <div key={i}>✗ {e}</div>) : "✓ 依赖校验通过"}
      </div>
      <div className="note" style={{ marginTop: 6 }}>
        说明：关闭「④ 图片卡片」需同时关闭「⑤ 平台存草稿」（图文平台存草稿必须带图）；关闭存草稿 = 只产出内容不进创作者中心。
        「⑥ 自动排期」按「总计划」里各平台的发布时段排进内容日历。
      </div>
    </div>
  );
}

// ============================================================ 流程诊断
function DiagnosePane() {
  const { withPending, isPending, showToast } = useApp();
  const [res, setRes] = useState<DiagnoseResult | null>(null);
  const [live, setLive] = useState(false);
  const run = () => withPending("diag", async () => {
    try { setRes(await api.diagnose(live)); } catch (e: any) { showToast(`诊断失败：${e.message}`, "error"); }
  });
  useEffect(() => { run(); /* 进页自动跑一次（不实测 LLM） */ }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const icon = { pass: "✓", warn: "●", block: "●" } as const;
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">⚑</span>8 点端到端诊断</div>
          <div className="panel-sub">
            {res ? `检查于 ${new Date(res.checked_at).toLocaleString("zh-CN")} · 总体 ${icon[res.overall]}` : "检查选题库、画像、登录、LLM、熔断、配额、数据库、Skills"}
          </div>
        </div>
        <div className="flex" style={{ gap: 10, alignItems: "center" }}>
          <label className="flex" style={{ gap: 4, alignItems: "center", fontSize: 12 }}>
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />实测 LLM（会产生一次调用）
          </label>
          <button className="btn sm primary" onClick={run} disabled={isPending("diag")}>{isPending("diag") ? "诊断中…" : "▶ 运行诊断"}</button>
        </div>
      </div>
      {!res && <div className="note">诊断中…</div>}
      {res && (
        <div className="table-wrap">
          <table>
            <thead><tr><th style={{ width: 40 }}>#</th><th>检查项</th><th style={{ width: 70 }}>结果</th><th>说明</th><th style={{ width: 90 }}>修复</th></tr></thead>
            <tbody>
              {res.items.map((it) => (
                <tr key={it.no}>
                  <td>{it.no}</td>
                  <td>{it.name}</td>
                  <td>{icon[it.status]}</td>
                  <td style={{ fontSize: 12 }}>{it.message}</td>
                  <td>{it.fix_page && it.status !== "pass" && (
                    <button className="btn sm" onClick={() => navigate(it.fix_page!, it.fix_tab ?? undefined)}>去修复 →</button>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================ 平台运行追踪
function TracePane({ status, runs }: { status: SystemStatus | null; runs: OrchestrationRun[] | null }) {
  if (!status) return <div className="note">加载中…</div>;
  const broken = status.circuit_broken_platforms ?? [];
  const noLogin = status.platforms_without_login ?? [];
  return (
    <div className="grid two">
      <div className="panel">
        <div className="panel-head"><div>
          <div className="panel-title"><span className="icon">☷</span>平台子流程</div>
          <div className="panel-sub">每个平台独立运行</div>
        </div></div>
        {(status.enabled_platforms ?? []).length === 0 ? (
          <EmptyHint next="到「系统设置」启用至少 1 个平台">暂无启用的平台</EmptyHint>
        ) : (
          <div className="list">
            {(status.enabled_platforms ?? []).map((p: Platform) => {
              const b = broken.includes(p), n = noLogin.includes(p);
              return (
                <div key={p} className="task-row">
                  <i className={`task-prio ${b ? "red" : n ? "orange" : "green"}`} />
                  <div className="task-main">
                    <div className="task-title">{PLATFORM_LABEL[p]}</div>
                    <div className="task-desc">{b ? "已熔断 · 停止所有运营动作" : n ? "无登录态 · 存草稿/采集/评论阻塞" : "登录态正常 · 可执行全部平台交互"}</div>
                  </div>
                  {b && <button className="btn sm" onClick={() => navigate("operations", "breaker")}>熔断详情</button>}
                  {!b && n && <button className="btn sm" onClick={() => navigate("accounts")}>去登录</button>}
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className="panel">
        <div className="panel-head"><div>
          <div className="panel-title"><span className="icon">↻</span>运行与恢复</div>
          <div className="panel-sub">活跃轮次 {status.active_rounds} · 评分模式 {status.scoring_mode}</div>
        </div></div>
        <div className="list">
          <div className="task-row"><i className="task-prio green" /><div className="task-main"><div className="task-title">中断任务</div><div className="task-desc">{status.recovery?.interrupted_tasks ?? 0} 个已重登</div></div></div>
          <div className="task-row"><i className="task-prio blue" /><div className="task-main"><div className="task-title">补登记采集</div><div className="task-desc">{status.recovery?.requeued_data_fetch?.length ?? 0} 篇重入列</div></div></div>
          <div className="task-row"><i className="task-prio orange" /><div className="task-main"><div className="task-title">卡住轮次</div><div className="task-desc">{status.recovery?.stale_rounds?.length ? `#${status.recovery.stale_rounds.join(", #")}` : "无"}</div></div></div>
          <div className="task-row"><i className="task-prio purple" /><div className="task-main"><div className="task-title">最近一轮</div><div className="task-desc">
            {runs && runs[0] ? `第 ${runs[0].round_number} 轮 · ${ROUND_STATUS_LABEL[runs[0].status] ?? runs[0].status}${runs[0].duration_seconds != null ? ` · 用时 ${runs[0].duration_seconds}s` : ""}` : "还没有轮次"}
          </div></div>
            {runs && runs[0] && <button className="btn sm" onClick={() => navigate("production", "rounds")}>查看</button>}
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================ Skills 注册表
function SkillsPane() {
  const { showToast, withPending, isPending } = useApp();
  const [customSkills, setCustomSkills] = useState<TenantSkillManifest[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [skillId, setSkillId] = useState("");
  const [skillName, setSkillName] = useState("");
  const [skillDescription, setSkillDescription] = useState("");
  const [skillCategory, setSkillCategory] = useState("custom");
  const [skillVersion, setSkillVersion] = useState("1.0.0");
  const [skillInputsJson, setSkillInputsJson] = useState("[]");
  const [skillOutputsJson, setSkillOutputsJson] = useState("[]");
  const [skillPermissions, setSkillPermissions] = useState<string[]>([]);

  const load = async () => {
    setLoading(true);
    try {
      const result = await api.tenantSkills();
      setCustomSkills(result.items ?? []);
      setLoadError("");
    } catch (e: any) {
      setLoadError(e?.message ?? "加载自定义 Skills 失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const register = async () => {
    await withPending("skill:register", async () => {
      try {
        await api.registerTenantSkill({
          skill_id: skillId.trim().toLowerCase(),
          name: skillName.trim(),
          version: skillVersion.trim(),
          description: skillDescription.trim(),
          category: skillCategory.trim().toLowerCase() || "custom",
          inputs: [],
          outputs: [],
          permissions: [],
        });
        showToast("Skill manifest 已登记，默认停用且不会执行代码", "success");
        setSkillId(""); setSkillName(""); setSkillDescription(""); setSkillVersion("1.0.0");
        setSkillInputsJson("[]"); setSkillOutputsJson("[]"); setSkillPermissions([]);
        await load();
      } catch (e: any) { showToast(`登记失败：${e?.message ?? e}`, "error"); }
    });
  };

  const toggle = async (skill: TenantSkillManifest) => {
    try {
      await withPending(`skill:toggle:${skill.skill_id}`, () =>
        api.setTenantSkillEnabled(skill.skill_id, !skill.enabled, skill.manifest_version));
      await load();
      showToast(skill.enabled ? "Skill 已停用" : "Skill 已启用（仅登记状态，不会执行）", "success");
    } catch (e: any) { showToast(`状态更新失败：${e?.message ?? e}`, "error"); }
  };

  const remove = async (skill: TenantSkillManifest) => {
    try {
      await withPending(`skill:delete:${skill.skill_id}`, () => api.deleteTenantSkill(skill.skill_id));
      await load();
      showToast("Skill 登记已删除", "success");
    } catch (e: any) { showToast(`删除失败：${e?.message ?? e}`, "error"); }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head"><div>
          <div className="panel-title"><span className="icon">▧</span>系统内置 Skills</div>
          <div className="panel-sub">{SKILLS.reduce((a, g) => a + g.rows.length, 0)} 个内置能力 · 只读元数据</div>
        </div></div>
        {SKILLS.map((g) => (
          <div key={g.group} style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: "var(--muted)", marginBottom: 6 }}>{g.group}</div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>能力</th><th>状态</th><th>说明</th></tr></thead>
                <tbody>
                  {g.rows.map((skill) => (
                    <tr key={skill.id}>
                      <td title={skill.src}>
                        <strong>{skill.name.replace(/（R\d+）/g, "").replace("Playwright ", "")}</strong>
                        <details className="skill-manifest-preview">
                          <summary>预览能力定义</summary>
                          <pre>{JSON.stringify({ schema_version: 1, skill_id: skill.id, name: skill.name, source: skill.src, status: skill.status === "OK" ? "available" : "limited", description: skill.note }, null, 2)}</pre>
                        </details>
                      </td>
                      <td><span className={`tag ${skill.status === "OK" ? "green" : "orange"}`}>{skill.status === "OK" ? "正常" : "部分可用"}</span></td>
                      <td style={{ fontSize: 12, color: "var(--muted)" }}>{skill.note.replace(/R\d+[：:]\s*/g, "")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>

      <div className="panel" style={{ marginTop: 14 }}>
        <div className="panel-head">
          <div><div className="panel-title">账号组自定义 Skills</div><div className="panel-sub">登记 manifest、预览和启停。当前仅管理元数据，不上传、不执行 Python / JavaScript 代码。</div></div>
          <button className="btn sm" onClick={() => void load()} disabled={loading}>↻ 刷新</button>
        </div>
        {loadError && <div className="note" style={{ color: "var(--red)" }}>加载失败：{loadError}</div>}
        <div className="skill-register-form">
          <label>Skill ID<input className="field" value={skillId} onChange={(e) => setSkillId(e.target.value)} placeholder="例如 brand_voice_check" /></label>
          <label>名称<input className="field" value={skillName} onChange={(e) => setSkillName(e.target.value)} maxLength={120} placeholder="能力名称" /></label>
          <label>版本<input className="field" value={skillVersion} onChange={(e) => setSkillVersion(e.target.value)} placeholder="1.0.0" /></label>
          <label>分类<input className="field" value={skillCategory} onChange={(e) => setSkillCategory(e.target.value)} placeholder="custom" /></label>
          <label className="skill-register-description">能力说明<input className="field" value={skillDescription} onChange={(e) => setSkillDescription(e.target.value)} maxLength={2000} placeholder="说明输入、输出和适用场景" /></label>
          <label>输入 Schema（JSON 数组）<textarea className="field" rows={3} value={skillInputsJson} onChange={(e) => setSkillInputsJson(e.target.value)} spellCheck={false} /></label>
          <label>输出 Schema（JSON 数组）<textarea className="field" rows={3} value={skillOutputsJson} onChange={(e) => setSkillOutputsJson(e.target.value)} spellCheck={false} /></label>
          <div className="skill-permissions">
            <span>只读权限声明</span>
            {(["read_content", "read_plan", "read_drafts"] as const).map((permission) => (
              <label key={permission}><input type="checkbox" checked={skillPermissions.includes(permission)} onChange={(e) => setSkillPermissions((old) => e.target.checked ? [...old, permission] : old.filter((p) => p !== permission))} />{permission}</label>
            ))}
          </div>
          <button className="btn primary" disabled={!skillId.trim() || !skillName.trim() || isPending("skill:register")} onClick={register}>
            {isPending("skill:register") ? "登记中…" : "登记 Skill"}
          </button>
        </div>
        {loading ? <div className="empty-state">正在加载登记表…</div> : customSkills.length === 0 ? (
          <EmptyHint next="填写上方 manifest 元数据后登记">暂无自定义 Skills</EmptyHint>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Skill</th><th>版本</th><th>分类</th><th>权限</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{customSkills.map((skill) => (
                <tr key={skill.skill_id}>
                  <td><strong>{skill.name}</strong><div className="tiny">{skill.skill_id}</div><div className="tiny">{skill.description}</div><details className="skill-manifest-preview"><summary>预览 Manifest</summary><pre>{JSON.stringify(skill, null, 2)}</pre></details></td>
                  <td>{skill.version}</td>
                  <td>{skill.category}</td>
                  <td>{skill.permissions.length ? skill.permissions.join("、") : "无"}</td>
                  <td><span className={skill.enabled ? "tag green" : "tag gray"}>{skill.enabled ? "已启用（仅登记）" : "已停用"}</span></td>
                  <td><div className="skill-row-actions">
                    <button className="btn sm" disabled={isPending(`skill:toggle:${skill.skill_id}`)} onClick={() => void toggle(skill)}>{skill.enabled ? "停用" : "启用"}</button>
                    <button className="btn sm" disabled={skill.enabled || isPending(`skill:delete:${skill.skill_id}`)} onClick={() => void remove(skill)}>删除</button>
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <div className="note" style={{ marginTop: 10 }}>安全限制：登记记录按账号组隔离；新建默认停用；权限仅允许读取类声明；启用不代表具备执行能力，Agent 当前不会加载或运行用户代码。</div>
      </div>
    </>
  );
}

// ============================================================ AI 审计链
function AuditPane() {
  const [aiCalls, setAiCalls] = useState<AiCallRecord[] | null>(null);
  const [aiCallsError, setAiCallsError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<UserOverrideRecord[] | null>(null);
  const [overridesError, setOverridesError] = useState<string | null>(null);
  const [redlines, setRedlines] = useState<RedlineRecord[] | null>(null);
  const [redlinesError, setRedlinesError] = useState<string | null>(null);
  const [auditTab, setAuditTab] = useState<"ai" | "override" | "redline">("ai");

  const loadAudit = async () => {
    setAiCallsError(null); setOverridesError(null); setRedlinesError(null);
    try { setAiCalls(await api.aiCalls(200)); } catch (e: any) { setAiCallsError(e.message); }
    try { setOverrides(await api.userOverride(200)); } catch (e: any) { setOverridesError(e.message); }
    try { setRedlines(await api.redlineHistory(200)); } catch (e: any) { setRedlinesError(e.message); }
  };
  useEffect(() => { loadAudit(); }, []);

  return (
      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">☷</span>AI 证据审计链</div>
            <div className="panel-sub">LLM 调用记录 · 人工覆盖痕迹 · 终极目标红线拦截历史</div>
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
              <EmptyHint next="编辑内容时选择「驳回」或「重新过目标检查」后自动留痕">还没有人工覆盖痕迹</EmptyHint>
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
              <EmptyHint next="选题或文案违反红线时会自动拦截并记录">还没有红线拦截历史 ✓</EmptyHint>
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
                          {r.guard_outcome === "fail" ? <span className="tag red">✕ FAIL</span>
                           : r.guard_outcome === "warn" ? <span className="tag orange">! WARN</span>
                           : <span className="tag gray">{r.guard_outcome}</span>}
                        </td>
                        <td style={{ maxWidth: 240, fontSize: 12 }}>{r.guard_reason ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div style={{ fontSize: 11, color: "var(--subtle)", marginTop: 6 }}>
                  ✦ 终极目标检查器 → 违反账号定位 → 拦截打回；拦截历史永久审计可追溯
                </div>
              </div>
            )}
          </>
        )}
      </div>
  );
}
