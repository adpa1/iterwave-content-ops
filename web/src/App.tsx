import { useEffect, useState } from "react";
import Sidebar, { NAV_ITEMS } from "./Sidebar";
import { AppProvider, useApp } from "./AppContext";
import Dashboard from "./pages/Dashboard";
import HotTopics from "./pages/HotTopics";
import Goals from "./pages/Goals";
import Production from "./pages/Production";
import Operations from "./pages/Operations";
import Calendar from "./pages/Calendar";
import Analytics from "./pages/Analytics";
import Gaps from "./pages/Gaps";
import Assets from "./pages/Assets";
import Agent from "./pages/Agent";
import Onboarding from "./pages/Onboarding";
import Settings from "./pages/Settings";

function Router() {
  // 初始读 URL param，之后 onChange 同步写 URL（demo 风格）
  const getInitial = () => {
    const m = new URLSearchParams(window.location.search).get("page");
    if (m && NAV_ITEMS.some((n) => n.page === m)) return m;
    return "dashboard";
  };
  const [page, setPageState] = useState<string>(getInitial);
  const { status } = useApp();

  const setPage = (p: string) => {
    setPageState(p);
    const url = new URL(window.location.href);
    url.searchParams.set("page", p);
    window.history.replaceState({}, "", url.toString());
  };

  useEffect(() => {
    const onPop = () => {
      const m = new URLSearchParams(window.location.search).get("page");
      if (m && NAV_ITEMS.some((n) => n.page === m)) setPageState(m);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const render = () => {
    switch (page) {
      case "dashboard": return <Dashboard />;
      case "hot": return <HotTopics />;
      case "goals": return <Goals />;
      case "production": return <Production />;
      case "operations": return <Operations />;
      case "calendar": return <Calendar />;
      case "analytics": return <Analytics />;
      case "gaps": return <Gaps />;
      case "assets": return <Assets />;
      case "agent": return <Agent />;
      case "onboarding": return <Onboarding />;
      case "settings": return <Settings />;
      default: return <Dashboard />;
    }
  };

  const currentNav = NAV_ITEMS.find((n) => n.page === page);

  return (
    <div className="app">
      <Sidebar
        current={page}
        onChange={setPage}
        brokenPlatforms={status?.circuit_broken_platforms || []}
      />
      <main className="content">
        <div style={{ marginBottom: 18, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <div style={{ fontSize: 11, color: "var(--muted)" }}>
            {currentNav?.section} <span style={{ margin: "0 6px" }}>·</span>{" "}
            <strong style={{ color: "var(--text)" }}>{currentNav?.label}</strong>
            {status?.scoring_mode && <span style={{ marginLeft: 14, color: "var(--subtle)" }}>
              · 评分模式 <strong style={{ color: "var(--teal)" }}>{status.scoring_mode}</strong>
            </span>}
          </div>
          {status && (
            <div className="flex wrap" style={{ gap: 6 }}>
              {status.enabled_platforms.map((p) => (
                <span key={p} className="tag blue" style={{ padding: "3px 9px", fontSize: 11 }}>
                  {p === "xhs" ? "📕 小红书" : p === "douyin" ? "🎵 抖音" : p === "wechat_channels" ? "💚 视频号" : "⚡ 快手"}
                </span>
              ))}
              {status.circuit_broken_platforms.length > 0 && (
                <span className="tag red">🚨 {status.circuit_broken_platforms.length} 平台熔断</span>
              )}
            </div>
          )}
        </div>
        {render()}
      </main>
    </div>
  );
}

// useState 在函数组件中必须显式 import
// (见文件顶部 import { useEffect, useState } from "react")

export default function App() {
  return (
    <AppProvider>
      <Router />
    </AppProvider>
  );
}
