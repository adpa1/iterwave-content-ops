/**
 * R20 · Demo 边界标注
 *
 * 五个等级的含义（AGENTS.md R20）：
 *   🔴 正式发布阻塞  完全不能执行真实动作
 *   🟠 演示值待接入  数字是演示用的
 *   🟡 原型加速      真实频率与界面声明不同（如 30min → 2s）
 *   🔵 会话内保持    状态在浏览器内存，刷新丢失
 *   ⚪ 不发请求      不会真实发请求 / 第三方可能限流
 */
type DemoLevel = "blocked" | "demo" | "accelerated" | "session" | "no_request";

const LEVEL_META: Record<DemoLevel, { icon: string; color: string; bg: string; label: string }> = {
  blocked: { icon: "🔴", color: "#f85149", bg: "rgba(248,81,73,.12)", label: "正式发布阻塞" },
  demo: { icon: "🟠", color: "#d29922", bg: "rgba(210,153,34,.12)", label: "演示值待接入" },
  accelerated: { icon: "🟡", color: "#e3b341", bg: "rgba(227,179,65,.12)", label: "原型加速" },
  session: { icon: "🔵", color: "#58a6ff", bg: "rgba(88,166,255,.12)", label: "会话内保持" },
  no_request: { icon: "⚪", color: "#8b949e", bg: "rgba(139,148,158,.12)", label: "不发请求" },
};

export default function DemoBadge({
  level, text,
}: { level: DemoLevel; text: string }) {
  const meta = LEVEL_META[level];
  return (
    <span
      title={meta.label}
      style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        fontSize: 11, lineHeight: 1.5, padding: "2px 8px", borderRadius: 999,
        color: meta.color, background: meta.bg,
        border: `1px solid ${meta.color}33`, whiteSpace: "nowrap",
      }}
    >
      <span>{meta.icon}</span>
      <span>{text}</span>
    </span>
  );
}
