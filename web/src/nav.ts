// ===== 页面跳转（全站统一）=====
// URL 形如 ?page=operations&tab=scheduler；子 tab 由各页面用 useUrlTab 读取。
import { useEffect, useState } from "react";
import { NAV_ITEMS } from "./Sidebar";

export function navigate(page: string, tab?: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("page", page);
  if (tab) url.searchParams.set("tab", tab);
  else url.searchParams.delete("tab");
  window.history.pushState({}, "", url.toString());
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** 读 / 写当前页面的子 tab（同步到 URL，刷新和跨页跳转都能落到指定 tab） */
export function useUrlTab<T extends string>(valid: readonly T[], fallback: T): [T, (t: T) => void] {
  const read = (): T => {
    const t = new URLSearchParams(window.location.search).get("tab") as T | null;
    return t && valid.includes(t) ? t : fallback;
  };
  const [tab, setTabState] = useState<T>(read);
  useEffect(() => {
    const onPop = () => setTabState(read());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const setTab = (t: T) => {
    setTabState(t);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", t);
    window.history.replaceState({}, "", url.toString());
  };
  return [tab, setTab];
}

/** 文案里的「页面名」→ 页面 key（EmptyHint 等提示自动变成可点击跳转） */
const LABEL_ALIASES: Record<string, { page: string; tab?: string }> = {
  "系统设置": { page: "settings" },
  "设置": { page: "settings" },
  "账号与登录": { page: "accounts" },
  "内容生产": { page: "production" },
  "生产线": { page: "production", tab: "pipeline" },
  "③ 生产线": { page: "production", tab: "pipeline" },
  "总计划": { page: "production", tab: "plan" },
  "内容资产": { page: "assets" },
  "选题库": { page: "assets", tab: "topics" },
  "热点雷达": { page: "hot" },
  "目标与策略": { page: "goals" },
  "运营控制台": { page: "operations" },
  "定时任务": { page: "operations", tab: "scheduler" },
  "任务追踪": { page: "operations", tab: "tasks" },
  "数据复盘": { page: "analytics" },
  "缺口与实验": { page: "gaps" },
  "内容日历": { page: "calendar" },
  "运营总览": { page: "dashboard" },
  "Agent 编排": { page: "agent" },
  "流程诊断": { page: "agent", tab: "diagnose" },
  "新手引导": { page: "onboarding" },
};

export function resolvePageLabel(label: string): { page: string; tab?: string } | null {
  const clean = label.split(/\s*[→>]\s*/)[0].trim();
  const sub = label.split(/\s*[→>]\s*/)[1]?.trim();
  if (sub && LABEL_ALIASES[sub] && LABEL_ALIASES[sub].page === (LABEL_ALIASES[clean]?.page ?? "")) {
    return LABEL_ALIASES[sub];
  }
  if (LABEL_ALIASES[clean]) return LABEL_ALIASES[clean];
  const hit = NAV_ITEMS.find((n) => n.label === clean || n.label.startsWith(clean));
  return hit ? { page: hit.page } : null;
}
