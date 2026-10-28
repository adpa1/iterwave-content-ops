/**
 * G-18 · 统一空态模板
 *
 * 空态不能只写「暂无数据」，必须带上下一步可执行动作：
 *   <EmptyHint next="到「内容生产」创建第一轮">暂无轮次记录</EmptyHint>
 *
 * next 里的「页面名」会自动变成可点击跳转（如「内容资产 → 选题库」「定时任务」）。
 * 也可以显式传 to / tab 指定跳转目标。
 */
import React from "react";
import { navigate, resolvePageLabel } from "../nav";

interface EmptyHintProps {
  children: React.ReactNode;
  next: string;
  className?: string;
  /** 显式跳转目标（不传则从 next 里的「」自动识别） */
  to?: string;
  tab?: string;
}

function renderNext(next: string, to?: string, tab?: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  const re = /「([^」]+)」/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let linked = false;
  while ((m = re.exec(next))) {
    parts.push(next.slice(last, m.index));
    const target = !linked ? (to ? { page: to, tab } : resolvePageLabel(m[1])) : null;
    if (target) {
      linked = true;
      parts.push(
        <button key={m.index} type="button" className="link-btn"
          onClick={() => navigate(target.page, target.tab)}>
          「{m[1]}」
        </button>,
      );
    } else {
      parts.push(`「${m[1]}」`);
    }
    last = m.index + m[0].length;
  }
  parts.push(next.slice(last));
  if (!linked && to) {
    parts.push(" ", <button key="go" type="button" className="link-btn" onClick={() => navigate(to, tab)}>前往 →</button>);
  }
  return parts;
}

export default function EmptyHint({ children, next, className = "empty", to, tab }: EmptyHintProps) {
  return (
    <div className={className || undefined} style={{ padding: "24px 16px", textAlign: "center" }}>
      <div style={{ fontSize: 13, color: "var(--subtle)" }}>{children}</div>
      <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted)" }}>
        下一步建议：{renderNext(next, to, tab)}
      </div>
    </div>
  );
}
