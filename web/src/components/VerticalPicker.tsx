import { useEffect, useState } from "react";
import { api, type Vertical } from "../api";

/**
 * 行业模板选择：只把模板内容填进表单（用户确认后再保存），不直接改配置。
 * 平台不绑定任何垂类，模板只是起点。
 */
export default function VerticalPicker({ onApply }: { onApply: (v: Vertical) => void }) {
  const [items, setItems] = useState<Vertical[]>([]);
  const [sel, setSel] = useState("");
  useEffect(() => { api.verticals().then((r) => setItems(r.items)).catch(() => {}); }, []);
  const cur = items.find((v) => v.id === sel);
  if (!items.length) return null;
  return (
    <div className="vertical-picker">
      <label className="form-label" htmlFor="verticalSel">从行业模板开始（可选）</label>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select id="verticalSel" className="field" style={{ width: 200 }} value={sel} onChange={(e) => setSel(e.target.value)}>
          <option value="">选择行业…</option>
          {items.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
        </select>
        <button type="button" className="btn sm" disabled={!cur} onClick={() => cur && onApply(cur)}>填入表单</button>
        <span className="tiny">填入后可随意修改，点保存才生效</span>
      </div>
      {cur && (
        <div className="tiny" style={{ marginTop: 6, lineHeight: 1.7 }}>
          {cur.persona_keywords.length > 0 && <>关键词：{cur.persona_keywords.join("、")}；</>}
          {cur.dimensions.length > 0 && <>建议选题维度：{cur.dimensions.join("、")}；</>}
          {cur.redline_keywords.length > 0 && <>会追加 {cur.redline_keywords.length} 个合规红线词。</>}
          {cur.compliance_note && <div style={{ color: "var(--orange)" }}>⚠ {cur.compliance_note}</div>}
        </div>
      )}
    </div>
  );
}
