import { Platform, platformMeta, ALL_PLATFORMS } from "./platforms";

export interface NavItem {
  page: string;
  label: string;
  /** demo 的克制符号体系（▦ ✳ ◎ ▤ ▣ ⌁ ⎇ ⚙），不用 emoji */
  icon: string;
  section: string;
  /** 不在侧栏出现（如首启向导，入口在顶栏） */
  hidden?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { page: "dashboard",  label: "运营总览",   icon: "▦", section: "指挥与发现" },
  { page: "hot",        label: "热点雷达",   icon: "✳", section: "指挥与发现" },
  { page: "goals",      label: "目标与策略", icon: "◎", section: "指挥与发现" },

  { page: "production", label: "内容生产",   icon: "▤", section: "内容作战" },
  { page: "calendar",   label: "内容日历",   icon: "▦", section: "内容作战" },
  { page: "assets",     label: "内容资产",   icon: "▣", section: "内容作战" },

  { page: "operations", label: "运营控制台", icon: "⌁", section: "运营与归因" },
  { page: "analytics",  label: "数据复盘",   icon: "⌁", section: "运营与归因" },
  { page: "gaps",       label: "缺口与实验", icon: "◇", section: "运营与归因" },

  { page: "agent",      label: "Agent 编排与诊断", icon: "⎇", section: "Agent 中枢" },
  { page: "accounts",   label: "账号与登录", icon: "◉", section: "Agent 中枢" },
  { page: "settings",   label: "系统设置",   icon: "⚙", section: "Agent 中枢" },
  { page: "onboarding", label: "新手引导 / 快速配置", icon: "☷", section: "Agent 中枢", hidden: true },
];

interface Props {
  current: string;
  onChange: (page: string) => void;
  brokenPlatforms: Platform[];
  loggedIn: Platform[];
  enabled: Platform[];
  activeRounds?: number;
  /** 状态是否已加载；未加载时显示「检测中」，不要先显示「未登录」吓人 */
  loaded?: boolean;
}

export default function Sidebar({ current, onChange, brokenPlatforms, loggedIn, enabled, activeRounds, loaded = true }: Props) {
  const sections: Record<string, NavItem[]> = {};
  NAV_ITEMS.filter((n) => !n.hidden).forEach((item) => {
    (sections[item.section] = sections[item.section] || []).push(item);
  });

  const countFor = (page: string): number | null => {
    if (page === "operations" && brokenPlatforms.length) return brokenPlatforms.length;
    if (page === "production" && activeRounds) return activeRounds;
    if (page === "accounts") {
      if (!loaded) return null;
      const missing = enabled.filter((p) => !loggedIn.includes(p)).length;
      return missing || null;
    }
    return null;
  };

  const platforms = enabled.length ? enabled : ALL_PLATFORMS;

  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">I</div>
        <div>
          <div className="brand-name">IterWave</div>
          <div className="brand-sub">MULTI-PLATFORM AGENT</div>
        </div>
      </div>

      <div className="workspace">
        <div className="w-dot" aria-hidden="true" />
        <div>
          <div className="w-name">本地工作区</div>
          <div className="w-sub">{platforms.length} 个平台 · 1 个运营 Agent</div>
        </div>
      </div>

      <nav aria-label="页面">
        {Object.entries(sections).map(([section, items]) => (
          <div key={section} role="group" aria-label={section}>
            <div className="group-label">{section}</div>
            {items.map((item) => {
              const count = countFor(item.page);
              const active = current === item.page;
              return (
                <button
                  type="button"
                  key={item.page}
                  className={`nav ${active ? "active" : ""}`}
                  aria-current={active ? "page" : undefined}
                  onClick={() => onChange(item.page)}
                  title={item.label}
                >
                  <span className="ni" aria-hidden="true">{item.icon}</span>
                  <span className="label">{item.label}</span>
                  {count != null && <span className="count">{count}</span>}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="side-bottom">
        <div className="health-title">平台账号状态</div>
        {platforms.map((p) => {
          const m = platformMeta(p);
          const isBroken = brokenPlatforms.includes(p);
          const isLogged = loggedIn.includes(p);
          const dot = !loaded ? "warn" : isBroken ? "warn" : isLogged ? "" : "bad";
          const label = !loaded ? "检测中" : isBroken ? "暂停中" : isLogged ? "已登录" : "未登录";
          return (
            <button
              type="button"
              key={p}
              className="health-row health-btn"
              onClick={() => onChange("accounts")}
              aria-label={`${m.label}：${label}，打开账号与登录`}
            >
              <span className="health-left"><i className={`dot ${dot}`} aria-hidden="true" />{m.label}</span>
              <span>{label}</span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
