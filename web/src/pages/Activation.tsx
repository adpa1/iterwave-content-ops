import { useEffect, useState } from "react";
import { api, ActivationSampleRun, ActivationStatus, Platform } from "../api";
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("内容空泛");
  const [toast, setToast] = useState("");

  const loadRun = async (id: string) => {
    const data = await api.activationSamples(id);
    setRun(data);
    if (data.run.status === "ready" || data.run.status === "failed" || data.run.status === "rejected") {
      setBusy(false);
    }
  };

  useEffect(() => {
    (async () => {
      try {
        const s = await api.activationStatus();
        setStatus(s);
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
      api.activationSamples(runId).then(setRun).catch((e) => setError(e.message));
    }, 1400);
    return () => window.clearInterval(timer);
  }, [runId, run?.run.status]);

  const start = async () => {
    if (busy) return;
    const description = accountDescription.trim();
    if (description.length < 10 || description.length > 300) {
      setError("账号定位请填写 10-300 个字符");
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
      if (result.status === "failed") setBusy(false);
    } catch (e: any) {
      setError(e?.message || "创建体验任务失败");
      setBusy(false);
    }
  };

  const copy = async (id: string, title: string, body: string, tags: string[]) => {
    try {
      await navigator.clipboard.writeText(`${title}\n\n${body}\n\n${tags.map((t) => t.startsWith("#") ? t : `#${t}`).join(" ")}`);
      if (runId) await api.activationSampleEvent(runId, id, "copied");
      setToast("已复制，复制行为已记录");
    } catch {
      setToast("复制失败：请检查浏览器剪贴板权限，未记录采用事件");
    }
  };

  const save = async (id: string) => {
    if (!runId) return;
    try {
      await api.activationSampleEvent(runId, id, "saved");
      await loadRun(runId);
      setToast("已保存到体验草稿区");
    } catch (e: any) {
      setError(e.message || "保存失败");
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
            id="activation-description" className="field" rows={3} maxLength={300}
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
            {!status?.model_ready && <span> · <button className="link-btn" onClick={() => navigate("settings")}>前往配置模型</button></span>}
          </div>
          <div className="panel-sub" style={{ marginTop: 6 }}>
            账号定位会发送给你配置的模型供应商用于生成；IterWave 不会把 API Key 返回浏览器。体验样稿不会发布到任何平台。
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
              <div className="panel-sub">任务状态：{run.run.status === "ready" ? "已完成" : run.run.status === "failed" ? "生成失败" : "正在生成，请稍候…"}</div>
            </div>
            <span className="tag">体验样稿 · 非正式生产内容</span>
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
    </>
  );
}
