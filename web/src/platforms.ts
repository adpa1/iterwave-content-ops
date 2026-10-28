/**
 * 平台元数据 —— 全站唯一来源（N3 修复）。
 *
 * 使用：
 *   import { platformMeta, ALL_PLATFORMS, Platform } from "../platforms";
 *   const meta = platformMeta("xhs");   // { label, color, icon }
 *
 * 添加新平台只需改此文件一处，所有页面自动同步。
 */

export type Platform = "xhs" | "douyin" | "kuaishou" | "wechat_channels";

export const ALL_PLATFORMS: Platform[] = ["xhs", "douyin", "kuaishou", "wechat_channels"];

export interface PlatformMeta {
  label: string;   // 中文显示名
  color: string;   // 品牌主色（hex）
  icon: string;    // 平台 emoji
}

const META: Record<Platform, PlatformMeta> = {
  xhs:             { label: "小红书", color: "#fe2c55", icon: "📕" },
  douyin:          { label: "抖音",   color: "#000000", icon: "🎵" },
  kuaishou:        { label: "快手",   color: "#ff6a00", icon: "⚡" },
  wechat_channels: { label: "视频号", color: "#07c160", icon: "💚" },
};

export function platformMeta(p: string | Platform): PlatformMeta {
  return (META[p as Platform] ?? { label: p, color: "#666", icon: "●" }) as PlatformMeta;
}

/** 旧 Sidebar 导出兼容（避免大规模改 import） */
export const PLATFORM_META = META;
