import { useEffect, useState } from "react";
import { api, ActivationSampleRun, ActivationSavedSample, ActivationStatus, Platform } from "../api";
import { navigate } from "../nav";

const PLATFORMS: { value: Platform; label: string }[] = [
  { value: "xhs", label: "小红书" },
  { value: "douyin", label: "抖音" },
  { value: "kuaishou", label: "快手" },
  { value: "wechat_channels", label: "视频号" },
];
const REASONS = ["定位不符", "内容空泛", "平台不适配", "事实不准确", "表达风格不符", "其他"];

export default function Activation() {
  const [status, setStatus] = useState<ActivationStatus | null>(null);
  const [accountDescription, setAccountDescription] = useState("");
  const [platform, setPlatform] = useState<Platform>("xhs");
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<ActivationSampleRun | null>(null);
  const [savedSamples, setSavedSamples] = useState<ActivationSavedSample[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("内容空泛");
  const [toast, setToast] = useState("");
  const [deleting, setDeleting] = useState(false);

  const loadRun = async (id: string) => {
    const data = await api.activationSamples(id);
    setRun(data);
    setBusy(["created", "generating", "quality_check"].includes(data.run.status));
  };

  const loadSaved = async () => {
    const data = await api.activationSavedSamples();
    setSavedSamples(data.items);
  };

  useEffect(() => {
    (async () => {
      try {
        await api.startActivation();
        const s = await api.activationStatus();
        setStatus(s);
        await loadSaved();
        if (s.latest_sample_run?.id) {
          setRunId(s.latest_sample_run.id);
          await loadRun(s.latest_sample_run.id);
        }
      } catch (e: any) {
        setError(e?.message || "快速体验状态加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (!runId || !run || !["created", "generating", "quality_check"].includes(run.run.status)) return;
    const timer = window.setInterval(() => {
      api.activationSamples(runId).then((data) => {
        setRun(data);
        if (["ready", "failed", "rejected"].includes(data.run.status)) setBusy(false);
      }).catch((e) => setError(e.message));
    }, 1400);
    return () => window.clearInterval(timer);
  }, [runId, run?.run.status]);

  const start = async () => {
    if (busy) return;
    const description = accountDescription.trim();
    const minChars = status?.activation_limits.account_description_min_chars ?? 10;
    const maxChars = status?.activation_limits.account_description_max_chars ?? 300;
    if (description.length < minChars || description.length > maxChars) {
      setError(`账号定位请填写 ${minChars}-${maxChars} 个字符`);
      return;
    }
    setBusy(true);
    setError("");
    setRun(null);
    try {
      const key = globalThis.crypto?.randomUUID?.() ?? `sample-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const result = await api.createActivationSamples({
        account_description: description, platform, idempotency_key: key,
      });
      setRunId(result.sample_run_id);
      await loadRun(result.sample_run_id);
      setStatus(await api.activationStatus());
      await loadSaved();
      if (result.status === "failed") setBusy(false);
    } catch (e: any) {
      setError(e?.message || "创建体验任务失败");
      setBusy(false);
    }
  };

  const copy = async (id: string, title: string, body: string, tags: string[], sourceRunId = runId) => {
    try {
      const text = `${title}\n\n${body}\n\n${tags.map((t) => t.startsWith("#") ? t : `#${t}`).join(" ")}`;
      let copied = false;
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        copied = true;
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        textarea.style.pointerEvents = "none";
        document.body.appendChild(textarea);
        textarea.select();
        copied = document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      if (!copied) throw new Error("浏览器未允许复制，请手动选择正文复制");
      if (sourceRunId) await api.activationSampleEvent(sourceRunId, id, "copied");
      setToast("已复制，复制行为已记录");
    } catch {
      setToast("复制失败：请手动选择正文复制，未记录复制事件");
    }
  };

  const save = async (id: string) => {
    if (!runId) return;
    try {
      await api.activationSampleEvent(runId, id, "saved");
      await loadRun(runId);
      await loadSaved();
      setToast("已保存到体验草稿区，可在下方「已保存样稿」中找回");
    } catch (e: any) {
      setError(e.message || "保存失败");
    }
  };

  const deleteRun = async () => {
    if (!runId || !window.confirm("删除本次体验记录及其样稿、反馈和相关事件？此操作不可撤销。")) return;
    setDeleting(true);
    try {
      await api.deleteActivationSamples(runId);
      setRun(null);
      setRunId(null);
      setToast("本次体验记录已删除");
      setStatus(await api.activationStatus());
    } catch (e: any) {
      setError(e?.message || "删除体验记录失败");
    } finally {
      setDeleting(false);
    }
  };

  const reject = async (id: string) => {
    if (!runId) return;
    try {
      await api.activationSampleEvent(runId, id, "rejected", reason);
      await loadRun(runId);
      setToast("反馈已记录，将用于改进体验");
    } catch (e: any) {
      setError(e.message || "提交反馈失败");
    }
  };

  if (loading) return <div className="loading"><div className="spinner" />加载快速体验…</div>;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">首次价值 · BYOK</div>
          <h1 className="page-title">快速体验</h1>
          <div className="page-desc">先看内容质量，再决定是否配置完整运营工作台。无需扫码登录平台。</div>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={() => navigate("onboarding")}>正式运营设置</button>
        </div>
      </div>

      <div className="panel" style={{ maxWidth: 860 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title">描述你的账号</div>
            <div className="panel-sub">用一句话说明你做什么、面向谁。系统会围绕三个不同创作角度生成样稿。</div>
          </div>
          <span className={status?.model_ready ? "tag green" : "tag orange"}>
            {status?.model_ready ? "自带模型已就绪" : "需要配置模型"}
          </span>
        </div>
        <div style={{ padding: "0 16px 16px" }}>
          <label className="form-label" htmlFor="activation-description">账号定位（10-300 字）</label>
          <textarea
            id="activation-description" className="field" rows={3}
            maxLength={status?.activation_limits.account_description_max_chars ?? 300}
            value={accountDescription} onChange={(e) => setAccountDescription(e.target.value)}
            placeholder="例如：分享给中小企业产品经理的 AI 工具实践、产品拆解和效率方法"
          />
          <div className="form-row" style={{ marginTop: 12 }}>
            <div style={{ flex: 1 }}>
              <label className="form-label" htmlFor="activation-platform">主战场平台</label>
              <select id="activation-platform" className="field" value={platform}
                onChange={(e) => setPlatform(e.target.value as Platform)}>
                {PLATFORMS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </div>
            <div style={{ alignSelf: "end" }}>
              <button className="btn primary" onClick={start} disabled={busy || !status?.model_ready}>
                {busy ? "正在生成…" : "生成 3 篇样稿"}
              </button>
            </div>
          </div>
          <div className="panel-sub" style={{ marginTop: 10 }}>
            {status?.model_message || "正在检查模型服务"}
            {status?.activation_limits && <span> · 今日剩余体验次数 {status.activation_limits.daily_runs_remaining}/{status.activation_limits.max_runs_per_day}</span>}
            {!status?.model_ready && <span> · <button className="link-btn" onClick={() => navigate("settings")}>前往配置模型</button></span>}
          </div>
          <div className="panel-sub" style={{ marginTop: 6 }}>
            账号定位会发送给你配置的模型供应商用于生成；IterWave 不会把 API Key 返回浏览器。体验样稿不会发布到任何平台。
          </div>
          <div className="panel-sub" style={{ marginTop: 6 }}>
            样稿由 AI 辅助生成，不会自动发布。发布前请人工核实事实、补充个人经验，并按平台要求声明 AI 生成内容。
          </div>
        </div>
      </div>

      {error && <div className="note" role="alert" style={{ color: "var(--red)", marginTop: 12 }}>{error}</div>}
      {toast && <div className="note" role="status" style={{ marginTop: 12 }}>{toast}</div>}

      {run && (
        <div style={{ marginTop: 18 }}>
          <div className="panel-head" style={{ padding: "0 0 10px" }}>
            <div>
              <div className="panel-title">本次体验结果</div>
              <div className="panel-sub">任务状态：{run.run.status === "ready" ? "已完成" : run.run.status === "failed" ? "生成失败" : "正在生成，请稍候…"} · 模型调用 {run.run.attempt_count ?? 0} 次</div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className="tag">体验样稿 · 非正式生产内容</span>
              {run.run.status !== "generating" && run.run.status !== "created" && run.run.status !== "quality_check" &&
                <button className="btn sm" onClick={deleteRun} disabled={deleting}>{deleting ? "删除中…" : "删除本次记录"}</button>}
            </div>
          </div>
          {["created", "generating", "quality_check"].includes(run.run.status) && (
            <div className="panel"><div className="loading"><div className="spinner" />正在生成并检查样稿，不显示虚构进度</div></div>
          )}
          {run.run.status === "failed" && (
            <div className="panel" style={{ padding: 16 }}>
              <strong>生成未完成</strong>
              <p>{run.run.error_message_safe || "模型生成失败，请检查模型配置后重试。"}</p>
              <button className="btn" onClick={start} disabled={!status?.model_ready}>重新生成</button>
            </div>
          )}
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
            {run.contents.map((item, index) => (
              <article className="panel" key={item.id} style={{ padding: 16 }}>
                <div className="panel-sub">样稿 {index + 1} · {item.angle}</div>
                <h3 style={{ margin: "8px 0 10px" }}>{item.title}</h3>
                <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7, maxHeight: 360, overflow: "auto" }}>{item.body}</div>
                <div className="panel-sub" style={{ marginTop: 10 }}>{item.hashtags.map((t) => t.startsWith("#") ? t : `#${t}`).join(" ")}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 14 }}>
                  <button className="btn sm" onClick={() => copy(item.id, item.title, item.body, item.hashtags)}>复制</button>
                  <button className="btn sm primary" onClick={() => save(item.id)} disabled={!!item.adopted_at}>
                    {item.adopted_at ? "已保存" : "保存到体验草稿"}
                  </button>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10 }}>
                  <select className="field" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="不采用原因">
                    {REASONS.map((r) => <option key={r}>{r}</option>)}
                  </select>
                  <button className="btn sm" onClick={() => reject(item.id)}>不合适</button>
                </div>
                {item.rejected_at && <div className="panel-sub" style={{ color: "var(--muted)", marginTop: 8 }}>已反馈：{item.rejection_reason}</div>}
              </article>
            ))}
          </div>
        </div>
      )}
      <section className="panel" style={{ marginTop: 20, maxWidth: 1120 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title">已保存样稿</div>
            <div className="panel-sub">跨体验批次保留，可随时找回；当前版本仍需人工复制到正式内容流程。</div>
          </div>
          <button className="btn sm" onClick={loadSaved}>刷新</button>
        </div>
        {savedSamples.length === 0 ? (
          <div className="panel-sub" style={{ padding: 16 }}>还没有保存的样稿。生成体验内容后，点击「保存到体验草稿」即可在这里找回。</div>
        ) : (
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12, padding: 12 }}>
            {savedSamples.map((item) => (
              <article className="task-row" key={item.id} style={{ display: "block", padding: 14 }}>
                <div className="panel-sub">{PLATFORMS.find((p) => p.value === item.platform)?.label || item.platform} · {item.angle} · 已保存于 {item.adopted_at}</div>
                <h3 style={{ margin: "8px 0" }}>{item.title}</h3>
                <div className="panel-sub" style={{ whiteSpace: "pre-wrap", maxHeight: 150, overflow: "auto" }}>{item.body}</div>
                <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                  <button className="btn sm" onClick={() => copy(item.id, item.title, item.body, item.hashtags, item.sample_run_id)}>复制</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
