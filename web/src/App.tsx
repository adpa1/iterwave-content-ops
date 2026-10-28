import { useEffect, useState } from "react";
import Sidebar, { NAV_ITEMS } from "./Sidebar";
import { AppProvider, useApp } from "./AppContext";
import { Platform } from "./platforms";
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
import Accounts from "./pages/Accounts";
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
      case "accounts": return <Accounts />;
      case "onboarding": return <Onboarding />;
      case "settings": return <Settings />;
      default: return <Dashboard />;
    }
  };

  const currentNav = NAV_ITEMS.find((n) => n.page === page);

  return (
    <div className="app-shell">
      <Topbar
        section={currentNav?.section ?? ""}
        label={currentNav?.label ?? ""}
        scoringMode={status?.scoring_mode}
      />
      <div className="app">
        <Sidebar
          current={page}
          onChange={setPage}
          brokenPlatforms={status?.circuit_broken_platforms || []}
          loggedIn={status?.logged_in_platforms || []}
          enabled={status?.enabled_platforms || []}
          noLogin={status?.platforms_without_login || []}
        />
        <main className="content">
          {render()}
        </main>
      </div>
    </div>
  );
}

// ===== 全局顶部栏 =====
function Topbar({ section, label, scoringMode }: { section: string; label: string; scoringMode?: string }) {
  const { status } = useApp();
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span style={{ fontWeight: 700, color: "var(--text)" }}>IterWave</span>
        <span className="sep">/</span>
        <span>{section}</span>
        <span className="sep">/</span>
        <strong>{label}</strong>
        {scoringMode && <span style={{ marginLeft: 8, color: "var(--teal)", fontSize: 11 }}>· {scoringMode}</span>}
      </div>
      <div className="topbar-search">
        <input placeholder="搜索内容 / 选题 / 任务（演示）" />
      </div>
      <div className="topbar-right">
        {status?.circuit_broken_platforms.length ? (
          <span className="tag red" title="熔断平台">🚨 熔断 {status.circuit_broken_platforms.length}</span>
        ) : null}
        <span className="user-chip"><span className="dot" />本地工作区</span>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Router />
    </AppProvider>
  );
}
