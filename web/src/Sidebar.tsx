import React from "react";
import { Platform } from "./api";

export interface NavItem {
  page: string;
  label: string;
  icon: string;
  section: string;
}

export const NAV_ITEMS: NavItem[] = [
  // 原型侧边栏 5 分组
  { page: "dashboard",  label: "工作台概览", icon: "◉", section: "主界面" },
  { page: "hot",        label: "热点雷达", icon: "✳", section: "主界面" },
  { page: "goals",      label: "目标与策略", icon: "◎", section: "主界面" },

  { page: "production", label: "内容生产", icon: "⎇", section: "运营流程" },
  { page: "operations", label: "运营控制台", icon: "⌁", section: "运营流程" },
  { page: "calendar",   label: "内容日历", icon: "◷", section: "运营流程" },

  { page: "analytics",  label: "数据分析", icon: "▦", section: "洞察" },
  { page: "gaps",       label: "缺口分析", icon: "△", section: "洞察" },

  { page: "agent",      label: "Agent 编排", icon: "⎇", section: "系统" },
  { page: "onboarding", label: "首次配置向导", icon: "✧", section: "系统" },
  { page: "settings",   label: "系统设置", icon: "⚙", section: "系统" },

  { page: "assets",     label: "内容资产", icon: "☷", section: "资产" },
];

const PLATFORM_META: Record<Platform, { label: string; color: string; icon: string }> = {
  xhs: { label: "小红书", color: "#fe2c55", icon: "📕" },
  douyin: { label: "抖音", color: "#000000", icon: "🎵" },
  kuaishou: { label: "快手", color: "#ff6a00", icon: "⚡" },
  wechat_channels: { label: "视频号", color: "#07c160", icon: "💚" },
};

export function platformMeta(p: Platform) {
  return PLATFORM_META[p] || { label: p, color: "#666", icon: "●" };
}

interface Props {
  current: string;
  onChange: (page: string) => void;
  brokenPlatforms: Platform[];
}

export default function Sidebar({ current, onChange, brokenPlatforms }: Props) {
  const sections: Record<string, NavItem[]> = {};
  NAV_ITEMS.forEach((item) => {
    (sections[item.section] = sections[item.section] || []).push(item);
  });

  return (
    <aside className="sidebar">
      {/* Brand block — 原型样式 */}
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
            <div style={{ fontSize: 10, color: "#8fa4bf", marginTop: 1 }}>四平台图文半自动运营</div>
          </div>
        </div>
        <div style={{
          background: "#20314a", border: "1px solid #314763",
          borderRadius: 9, padding: "10px", margin: "0 5px 14px",
          display: "flex", alignItems: "center", gap: 8,
        }}>
          <div style={{
            width: 8, height: 8, borderRadius: "50%", background: "#53d3ad",
            boxShadow: "0 0 0 4px #284b58",
          }} />
          <div style={{ fontSize: 12, color: "#fff", fontWeight: 600 }}>本地运行 · 后端 http://127.0.0.1:8000</div>
        </div>
      </div>

      {/* Nav sections */}
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

      <div className="sidebar-bottom">
        <div>⚡ v0.1.0 · MVP 原型</div>
        <div style={{ marginTop: 4, opacity: 0.55 }}>启动前必读：AGENTS.md 红线规则</div>
      </div>
    </aside>
  );
}
