import { useEffect, useState } from "react";
import { api, type IndustryPreset } from "../api";
import { useApp } from "../AppContext";

/** 行业模板：一键填画像示例 / 建议维度 / 合规红线，并提示平台限制 */
export default function IndustryPresetPicker({ onApplied }: { onApplied: () => void }) {
  const { showToast } = useApp();
  const [presets, setPresets] = useState<IndustryPreset[]>([]);
  const [active, setActive] = useState<IndustryPreset | null>(null);
  const [busy, setBusy] = useState("");
  const [applied, setApplied] = useState<{ dimensions: string[]; notes: Record<string, string> } | null>(null);

  useEffect(() => {
    let alive = true;
    api.presets()
      .then((res) => { if (alive) setPresets(res.items); })
      .catch((e) => showToast((e as Error).message, "error"));
    return () => { alive = false; };
  }, [showToast]);

  const apply = async (preset: IndustryPreset) => {
    if (!window.confirm(`应用「${preset.name}」模板？会更新定位、关键词、禁止方向并追加合规红线（可再修改）。`)) return;
    setBusy(preset.key);
    setActive(preset);
    try {
      const res = await api.applyPreset(preset.key);
      setApplied({ dimensions: res.dimensions, notes: res.platform_notes });
      showToast(res.message, "success");
      onApplied();
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const fillPlan = async () => {
    if (!applied) return;
    try {
      await api.adoptGaps(applied.dimensions);
      showToast("已写入母计划维度，下游内容会按新维度重排", "success");
    } catch (e) {
      showToast(`写入母计划失败：${(e as Error).message}`, "error");
    }
  };

  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">◇</span>行业模板</div>
          <div className="panel-sub">一键填入画像示例、建议维度与合规红线；所有字段都可以再改</div>
        </div>
      </div>
      <div className="flex wrap" style={{ gap: 8 }}>
        {presets.map((p) => (
          <button key={p.key} className="btn sm soft" type="button" disabled={busy === p.key}
            title={p.description} onClick={() => apply(p)}>
            {busy === p.key ? "应用中…" : p.name}
          </button>
        ))}
      </div>

      {active && applied && (
        <div className="note" style={{ marginTop: 12, lineHeight: 1.8 }}>
          <strong>{active.name}</strong> 已应用。建议维度：{applied.dimensions.join("、")}
          <button className="btn xs soft" type="button" style={{ marginLeft: 8 }} onClick={fillPlan}>
            填入母计划
          </button>
          {Object.entries(applied.notes).length > 0 && (
            <div style={{ marginTop: 6, color: "var(--muted)" }}>
              平台限制：
              {Object.entries(applied.notes).map(([k, v]) => (
                <div key={k}>· {k}：{v}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
