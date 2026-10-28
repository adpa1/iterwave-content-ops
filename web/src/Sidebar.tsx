import React from "react";
import { Platform, platformMeta } from "./platforms";

export interface NavItem {
  page: string;
  label: string;
  icon: string;
  section: string;
}

export const NAV_ITEMS: NavItem[] = [
  { page: "dashboard",  label: "运营总览", icon: "🏠", section: "指挥与发现" },
  { page: "hot",        label: "热点雷达", icon: "🔥", section: "指挥与发现" },
  { page: "goals",      label: "目标与策略", icon: "🎯", section: "指挥与发现" },
  { page: "assets",     label: "内容资产", icon: "📚", section: "指挥与发现" },

  { page: "production", label: "内容生产", icon: "📝", section: "内容作战" },
  { page: "calendar",   label: "内容日历", icon: "📅", section: "内容作战" },

  { page: "operations", label: "运营控制台", icon: "⚙️", section: "运营与复盘" },
  { page: "analytics",  label: "数据复盘", icon: "📊", section: "运营与复盘" },
  { page: "gaps",       label: "缺口与实验", icon: "🔬", section: "运营与复盘" },

  { page: "agent",      label: "工作流诊断", icon: "🔧", section: "Agent 与设置" },
  { page: "accounts",   label: "账号与登录", icon: "🔐", section: "Agent 与设置" },
  { page: "settings",   label: "账号与设置", icon: "⚙", section: "Agent 与设置" },
  { page: "onboarding", label: "首启配置向导", icon: "🚀", section: "Agent 与设置" },
];

interface Props {
  current: string;
  onChange: (page: string) => void;
  brokenPlatforms: Platform[];
  loggedIn: Platform[];
  enabled: Platform[];
  noLogin: Platform[];
}

export default function Sidebar({ current, onChange, brokenPlatforms, loggedIn, enabled, noLogin }: Props) {
  const sections: Record<string, NavItem[]> = {};
  NAV_ITEMS.forEach((item) => {
    (sections[item.section] = sections[item.section] || []).push(item);
  });

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "2px 6px 18px" }}>
          <div style={{
            width: 32, height: 32, borderRadius: 10,
            background: "linear-gradient(145deg, #6f8cff, #4263eb)",
            display: "grid", placeItems: "center",
            color: "#fff", fontWeight: 800, fontSize: 17,
            boxShadow: "0 5px 14px #172b65",
          }}>I</div>
          <div>
            <div style={{ fontWeight: 750, letterSpacing: ".2px", fontSize: 15, color: "#fff" }}>IterWave</div>
            <div style={{ fontSize: 10, color: "#8fa4bf", marginTop: 1 }}>AI 图文内容运营</div>
          </div>
        </div>
      </div>

      {Object.entries(sections).map(([section, items]) => (
        <div key={section} className="nav-section">
          <div className="nav-label">{section}</div>
          {items.map((item) => {
            const broken = item.page === "operations" && brokenPlatforms.length > 0;
            return (
              <div
                key={item.page}
                className={`nav-item ${current === item.page ? "active" : ""}`}
                onClick={() => onChange(item.page)}
                title={item.label}
              >
                <span className="icon">{item.icon}</span>
                {item.label}
                {broken && <span className="badge">{brokenPlatforms.length}</span>}
              </div>
            );
          })}
        </div>
      ))}

      {/* 四平台连接状态常驻 */}
      <SidebarPlatformStatus
        enabled={enabled}
        loggedIn={loggedIn}
        broken={brokenPlatforms}
        noLogin={noLogin}
      />
    </aside>
  );
}

function SidebarPlatformStatus({ enabled, loggedIn, broken, noLogin }: {
  enabled: Platform[]; loggedIn: Platform[]; broken: Platform[]; noLogin: Platform[];
}) {
  if (!enabled.length) return null;
  return (
    <div className="sidebar-platforms" title="四平台连接状态">
      {enabled.map((p) => {
        const m = platformMeta(p);
        const isBroken = broken.includes(p);
        const isNoLogin = noLogin.includes(p) || !loggedIn.includes(p);
        const cls = isBroken ? "broken" : isNoLogin ? "nologin" : "ok";
        const label = isBroken ? "熔断" : isNoLogin ? "无登录" : "正常";
        return (
          <span key={p} className={`plat-chip ${cls}`} title={`${m.label}：${label}`}>
            <span className="dot" />
            {m.label}
          </span>
        );
      })}
    </div>
  );
}
