/**
 * 账号定位与画像（多平台）。
 *
 * 不同平台登录的可能是不同账号、不同定位：
 * - 统一：一个画像、一个总目标，所有平台共用。
 * - 分平台：每个平台独立画像、独立目标，互不汇总。
 * - 总目标 + 分平台：一个总目标统领，各平台有自己的画像和子目标，并可配置平台间的协同（导流 / 素材复用 / 互推）。
 * 红线词库始终叠加全局，平台画像不能去掉安全底线。
 */
import { useEffect, useState } from "react";
import { api, type GoalMode, type PersonasView, type Platform, type PlatformLink, type PlatformPersonaConfig } from "../api";
import { useApp } from "../AppContext";
import { ALL_PLATFORMS, platformMeta } from "../platforms";

export const GOAL_MODE_META: Record<GoalMode, { title: string; desc: string }> = {
  unified: { title: "统一定位", desc: "所有平台是同一个账号人设，共用一个画像和一个总目标" },
  per_platform: { title: "分平台独立", desc: "各平台是不同定位的账号，画像和目标各自独立，不做汇总" },
  hybrid: { title: "总目标 + 分平台协同", desc: "一个总目标统领，各平台有自己的画像和子目标，可以互相导流促进" },
};

export const ROLE_LABELS: Record<string, string> = {
  "": "未设置",
  traffic: "引流",
  conversion: "转化",
  brand: "品牌",
  community: "社群互动",
  test: "试验新方向",
};

export const RELATION_LABELS: Record<PlatformLink["relation"], string> = {
  traffic: "导流到",
  reuse: "素材复用到",
  cross: "与之互推",
};

const EMPTY: PlatformPersonaConfig = {
  inherit_global: true, account_name: "", positioning: "", audience: "", role: "",
  persona_keywords: [], forbidden_directions: [], extra_redlines: [],
};

const split = (t: string) => t.split(/[,，、\n]/).map((s) => s.trim()).filter(Boolean);
const join = (a: string[]) => a.join("、");

type Draft = {
  mode: GoalMode;
  positioning: string;
  keywords: string;
  forbidden: string;
  platforms: Record<string, { inherit_global: boolean; account_name: string; positioning: string; audience: string; role: string; keywords: string; forbidden: string; redlines: string }>;
  links: PlatformLink[];
};

function toDraft(v: PersonasView): Draft {
  const platforms: Draft["platforms"] = {};
  for (const p of ALL_PLATFORMS) {
    const c = v.platforms[p]?.config ?? EMPTY;
    platforms[p] = {
      inherit_global: c.inherit_global, account_name: c.account_name, positioning: c.positioning,
      audience: c.audience, role: c.role, keywords: join(c.persona_keywords),
      forbidden: join(c.forbidden_directions), redlines: join(c.extra_redlines),
    };
  }
  return {
    mode: v.goal_mode, positioning: v.global.positioning ?? "",
    keywords: join(v.global.persona_keywords), forbidden: join(v.global.forbidden_directions),
    platforms, links: v.links ?? [],
  };
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

export default function PersonaMatrix({ onSaved }: { onSaved?: () => void }) {
  const { showToast, withPending, isPending } = useApp();
  const [view, setView] = useState<PersonasView | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openPlatform, setOpenPlatform] = useState<Platform | null>(null);

  const load = async () => {
    setErr(null);
    try {
      const v = await api.personas();
      setView(v);
      setDraft(toDraft(v));
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  };
  useEffect(() => { load(); }, []);

  if (err) return <div className="note" style={{ color: "var(--red)" }}>画像加载失败：{err} <button className="btn sm" onClick={load}>重试</button></div>;
  if (!view || !draft) return <div className="note">加载画像中…</div>;

  const setP = (p: string, patch: Partial<Draft["platforms"][string]>) =>
    setDraft((d) => d && ({ ...d, platforms: { ...d.platforms, [p]: { ...d.platforms[p], ...patch } } }));

  const save = async () => {
    const u: Record<string, unknown> = {};
    if (draft.mode !== view.goal_mode) u["ultimate_goal.goal_mode"] = draft.mode;
    if (draft.positioning.trim() !== (view.global.positioning ?? "")) u["ultimate_goal.positioning"] = draft.positioning.trim();
    if (!same(split(draft.keywords), view.global.persona_keywords)) u["ultimate_goal.persona_keywords"] = split(draft.keywords);
    if (!same(split(draft.forbidden), view.global.forbidden_directions)) u["ultimate_goal.forbidden_directions"] = split(draft.forbidden);
    for (const p of ALL_PLATFORMS) {
      const c = view.platforms[p]?.config ?? EMPTY;
      const d = draft.platforms[p];
      const k = (f: string) => `ultimate_goal.platform_personas.${p}.${f}`;
      if (d.inherit_global !== c.inherit_global) u[k("inherit_global")] = d.inherit_global;
      for (const f of ["account_name", "positioning", "audience", "role"] as const) {
        if (d[f].trim() !== (c[f] ?? "")) u[k(f)] = d[f].trim();
      }
      if (!same(split(d.keywords), c.persona_keywords)) u[k("persona_keywords")] = split(d.keywords);
      if (!same(split(d.forbidden), c.forbidden_directions)) u[k("forbidden_directions")] = split(d.forbidden);
      if (!same(split(d.redlines), c.extra_redlines)) u[k("extra_redlines")] = split(d.redlines);
    }
    if (JSON.stringify(draft.links) !== JSON.stringify(view.links)) u["ultimate_goal.platform_links"] = draft.links;
    if (Object.keys(u).length === 0) { showToast("没有需要保存的改动", "info"); return; }
    try {
      const r = await withPending("persona:save", () => api.patchConfig(u));
      if (!r) return;
      showToast(`✓ 已保存 ${r.changed.length} 项画像设置，下一次生成文案即按新画像执行`, "success");
      await load();
      onSaved?.();
    } catch (e: any) {
      showToast(`保存失败：${e?.message ?? e}`, "error");
    }
  };

  const multi = draft.mode !== "unified";
  const enabled = new Set(view.enabled_platforms);

  return (
    <div>
      <div className="form-label">目标模式</div>
      <div className="grid three" style={{ gap: 10, marginBottom: 16 }}>
        {(Object.keys(GOAL_MODE_META) as GoalMode[]).map((m) => (
          <button key={m} type="button" className={`mode-card ${draft.mode === m ? "active" : ""}`}
            onClick={() => setDraft({ ...draft, mode: m })}>
            <div className="mode-title">{GOAL_MODE_META[m].title}</div>
            <div className="mode-desc">{GOAL_MODE_META[m].desc}</div>
          </button>
        ))}
      </div>

      <div className="panel-sub" style={{ marginBottom: 8 }}>{multi ? "全局画像（各平台未单独设置时使用；红线词库对所有平台生效）" : "账号画像（所有平台共用）"}</div>
      <div className="grid three" style={{ gap: 10, marginBottom: 16 }}>
        <div>
          <label className="form-label">一句话定位</label>
          <input className="field" placeholder="如：帮职场人用 AI 提效的效率博主" value={draft.positioning}
            onChange={(e) => setDraft({ ...draft, positioning: e.target.value })} />
        </div>
        <div>
          <label className="form-label">定位关键词（顿号或逗号分隔）</label>
          <input className="field" value={draft.keywords} onChange={(e) => setDraft({ ...draft, keywords: e.target.value })} />
        </div>
        <div>
          <label className="form-label">禁止方向</label>
          <input className="field" value={draft.forbidden} onChange={(e) => setDraft({ ...draft, forbidden: e.target.value })} />
        </div>
      </div>

      {multi && (
        <>
          <div className="panel-sub" style={{ marginBottom: 8 }}>各平台账号画像</div>
          <div className="grid two" style={{ gap: 10, marginBottom: 16 }}>
            {ALL_PLATFORMS.map((p) => {
              const d = draft.platforms[p];
              const eff = view.platforms[p]?.effective;
              const open = openPlatform === p;
              return (
                <div key={p} className="persona-card">
                  <div className="flex between" style={{ alignItems: "center" }}>
                    <div className="flex" style={{ gap: 8, alignItems: "center" }}>
                      <span className="plat-badge" style={{ background: platformMeta(p).color }}>{platformMeta(p).icon}</span>
                      <b>{platformMeta(p).label}</b>
                      {d.account_name && <span className="metric-foot">· {d.account_name}</span>}
                      {!enabled.has(p) && <span className="tag gray">未启用</span>}
                      {d.role && <span className="tag blue">{ROLE_LABELS[d.role]}</span>}
                    </div>
                    <button type="button" className="btn sm" onClick={() => setOpenPlatform(open ? null : p)}>{open ? "收起" : "编辑"}</button>
                  </div>
                  <div className="metric-foot" style={{ marginTop: 6 }}>
                    {eff?.source === "platform" ? "独立画像" : eff?.source === "merged" ? "全局 + 本平台补充" : "使用全局画像"}
                    {eff?.positioning ? ` · ${eff.positioning}` : ""}
                  </div>
                  <div className="flex wrap" style={{ gap: 4, marginTop: 6 }}>
                    {(eff?.persona_keywords ?? []).slice(0, 8).map((k) => <span key={k} className="tag gray">{k}</span>)}
                  </div>
                  {open && (
                    <div className="grid two" style={{ gap: 8, marginTop: 10 }}>
                      <div>
                        <label className="form-label">账号名称</label>
                        <input className="field" value={d.account_name} onChange={(e) => setP(p, { account_name: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label">在矩阵中的角色</label>
                        <select className="field" value={d.role} onChange={(e) => setP(p, { role: e.target.value })}>
                          {Object.entries(ROLE_LABELS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                        </select>
                      </div>
                      <div style={{ gridColumn: "1 / -1" }}>
                        <label className="form-label">本平台定位</label>
                        <input className="field" placeholder="不填则沿用全局定位" value={d.positioning} onChange={(e) => setP(p, { positioning: e.target.value })} />
                      </div>
                      <div style={{ gridColumn: "1 / -1" }}>
                        <label className="form-label">目标受众</label>
                        <input className="field" placeholder="如：25-35 岁一线城市宝妈" value={d.audience} onChange={(e) => setP(p, { audience: e.target.value })} />
                      </div>
                      <div style={{ gridColumn: "1 / -1" }}>
                        <label className="gate-switch">
                          <input type="checkbox" checked={d.inherit_global} onChange={(e) => setP(p, { inherit_global: e.target.checked })} />
                          在全局画像基础上补充（取消勾选 = 本平台完全使用自己的画像）
                        </label>
                      </div>
                      <div>
                        <label className="form-label">本平台关键词</label>
                        <input className="field" value={d.keywords} onChange={(e) => setP(p, { keywords: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label">本平台禁止方向</label>
                        <input className="field" value={d.forbidden} onChange={(e) => setP(p, { forbidden: e.target.value })} />
                      </div>
                      <div style={{ gridColumn: "1 / -1" }}>
                        <label className="form-label">本平台额外红线词（叠加在全局红线之上）</label>
                        <input className="field" value={d.redlines} onChange={(e) => setP(p, { redlines: e.target.value })} />
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div className="flex between" style={{ alignItems: "center", marginBottom: 8 }}>
            <div className="panel-sub">平台协同关系（一个平台如何促进另一个平台）</div>
            <button type="button" className="btn sm" disabled={draft.links.length >= 12}
              onClick={() => setDraft({ ...draft, links: [...draft.links, { source: "xhs", target: "wechat_channels", relation: "traffic", note: "" }] })}>
              ＋ 添加关系
            </button>
          </div>
          {draft.links.length === 0 ? (
            <div className="note" style={{ marginBottom: 12 }}>还没有协同关系。比如「小红书种草 → 导流到 → 视频号直播转化」。</div>
          ) : (
            <div className="list" style={{ marginBottom: 12 }}>
              {draft.links.map((l, i) => (
                <div key={i} className="flex wrap" style={{ gap: 6, alignItems: "center" }}>
                  <select className="field" style={{ width: 110 }} value={l.source}
                    onChange={(e) => setDraft({ ...draft, links: draft.links.map((x, j) => j === i ? { ...x, source: e.target.value as Platform } : x) })}>
                    {ALL_PLATFORMS.map((p) => <option key={p} value={p}>{platformMeta(p).label}</option>)}
                  </select>
                  <select className="field" style={{ width: 120 }} value={l.relation}
                    onChange={(e) => setDraft({ ...draft, links: draft.links.map((x, j) => j === i ? { ...x, relation: e.target.value as PlatformLink["relation"] } : x) })}>
                    {Object.entries(RELATION_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                  <select className="field" style={{ width: 110 }} value={l.target}
                    onChange={(e) => setDraft({ ...draft, links: draft.links.map((x, j) => j === i ? { ...x, target: e.target.value as Platform } : x) })}>
                    {ALL_PLATFORMS.map((p) => <option key={p} value={p}>{platformMeta(p).label}</option>)}
                  </select>
                  <input className="field" style={{ flex: 1, minWidth: 160 }} placeholder="说明（选填）" value={l.note}
                    onChange={(e) => setDraft({ ...draft, links: draft.links.map((x, j) => j === i ? { ...x, note: e.target.value } : x) })} />
                  <button type="button" className="btn sm" onClick={() => setDraft({ ...draft, links: draft.links.filter((_, j) => j !== i) })}>删除</button>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <div className="flex" style={{ gap: 10, alignItems: "center" }}>
        <button className="btn primary sm" onClick={save} disabled={isPending("persona:save")}>
          {isPending("persona:save") ? "保存中…" : "保存画像设置"}
        </button>
        <span className="metric-foot">文案生成、质量检查、热点匹配和选题评分都会按各平台的生效画像执行</span>
      </div>
    </div>
  );
}
