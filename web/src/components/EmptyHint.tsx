/**
 * G-18 · 统一空态模板
 *
 * 空态不能只写「暂无数据」，必须带上下一步可执行动作：
 *   <EmptyHint next="到「内容生产」创建第一轮">暂无轮次记录</EmptyHint>
 *
 * 表格单元格内使用时可传 className=""（td 已有内边距），保持居中灰字风格。
 */
import React from "react";

interface EmptyHintProps {
  /** 第一行：场景说明，如「暂无运营日志」 */
  children: React.ReactNode;
  /** 第二行：下一步建议的具体可执行操作（不带「下一步建议：」前缀） */
  next: string;
  className?: string;
}

export default function EmptyHint({
  children,
  next,
  className = "empty",
}: EmptyHintProps) {
  return (
    <div className={className || undefined} style={{ padding: "24px 16px", textAlign: "center" }}>
      <div style={{ fontSize: 13, color: "var(--subtle)" }}>{children}</div>
      <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted)" }}>
        下一步建议：{next}
      </div>
    </div>
  );
}
