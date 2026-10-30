import { useEffect, useState } from "react";
import { api, type OpsTargetsView, type Platform } from "../api";
import { useApp } from "../AppContext";

const PLATFORMS: { key: Platform; label: string }[] = [
  { key: "xhs", label: "小红书" },
  { key: "douyin", label: "抖音" },
  { key: "kuaishou", label: "快手" },
  { key: "wechat_channels", label: "视频号" },
];

const EMPTY: OpsTargetsView = Object.fromEntries(
  PLATFORMS.map(({ key }) => [key, { active_comments: [], follows: [] }]),
);

function commentsText(items: { url: string; content: string }[]): string {
  return items.map((item) => `${item.url} | ${item.content}`).join("\n");
}

export default function OpsTargetsEditor() {
  const { showToast } = useApp();
  const [targets, setTargets] = useState<OpsTargetsView>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const result = await api.opsTargets();
      setTargets({ ...EMPTY, ...result.items });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const save = async () => {
    const normalized: OpsTargetsView = { ...EMPTY };
    for (const { key, label } of PLATFORMS) {
      const current = targets[key] ?? EMPTY[key];
      const comments = current.active_comments;
      for (const item of comments) {
        if (!item.url.trim() || !item.content.trim()) {
          showToast(`${label} 评论目标的 URL 和评论内容都不能为空`, "error");
          return;
        }
      }
      normalized[key] = {
        active_comments: comments.map((item) => ({ url: item.url.trim(), content: item.content.trim() })),
        follows: current.follows.map((item) => item.trim()).filter(Boolean),
      };
    }
    setBusy(true);
    try {
      const result = await api.saveOpsTargets(normalized);
      setTargets(result.items);
      showToast(result.message, "success");
    } catch (e) {
      showToast(`保存主动运营目标失败：${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="note">主动运营目标加载中…</div>;
  if (error) return (
    <div className="panel">
      <div className="note" style={{ color: "var(--red)" }}>主动运营目标加载失败：{error}</div>
      <button className="btn sm" onClick={load}>重试</button>
    </div>
  );

  return (
    <div className="panel" style={{ gridColumn: "1 / -1" }}>
      <div className="panel-head">
        <div>
          <div className="panel-title">主动运营目标</div>
          <div className="panel-sub">
            目标按账号组保存在 SQLite。空列表不派发动作；评论目标每行填写「主页 URL | 评论内容」，关注目标每行填写主页 URL 或用户 ID。
          </div>
        </div>
        <button className="btn primary sm" onClick={save} disabled={busy}>
          {busy ? "保存中…" : "保存目标"}
        </button>
      </div>
      <div className="grid two">
        {PLATFORMS.map(({ key, label }) => {
          const current = targets[key] ?? EMPTY[key];
          return (
            <div key={key} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{label}</div>
              <label className="form-label">主动评论目标（每行：URL | 评论内容）</label>
              <textarea
                className="field"
                rows={5}
                value={commentsText(current.active_comments)}
                placeholder={"https://平台主页链接 | 写一条有具体观点的评论"}
                onChange={(event) => {
                  const active_comments = event.target.value.split("\n").filter((line) => line.trim()).map((line) => {
                    const index = line.indexOf("|");
                    return index < 0
                      ? { url: line.trim(), content: "" }
                      : { url: line.slice(0, index).trim(), content: line.slice(index + 1).trim() };
                  });
                  setTargets((prev) => ({
                    ...prev, [key]: { ...current, active_comments },
                  }));
                }}
              />
              <label className="form-label" style={{ marginTop: 10 }}>关注目标（每行一个 URL 或用户 ID）</label>
              <textarea
                className="field"
                rows={3}
                value={current.follows.join("\n")}
                placeholder={"https://平台用户主页\nuser_id"}
                onChange={(event) => setTargets((prev) => ({
                  ...prev, [key]: {
                    ...current,
                    follows: event.target.value.split("\n").map((line) => line.trim()).filter(Boolean),
                  },
                }))}
              />
            </div>
          );
        })}
      </div>
      <div className="metric-foot" style={{ marginTop: 8 }}>
        评论目标仍会经过终极目标检查；本设置只维护目标，不会绕过配额、运营时段或人工发布门禁。
      </div>
    </div>
  );
}
