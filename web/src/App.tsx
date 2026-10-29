import { Component, useEffect, useState, type ReactNode } from "react";
import Sidebar, { NAV_ITEMS } from "./Sidebar";
import { AppProvider, useApp } from "./AppContext";
import { api } from "./api";
import { platformMeta } from "./platforms";
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
  const { status, auth } = useApp();

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

  if (!auth) {
    return <div className="app-loading">正在检测访问状态…</div>;
  }
  if (auth.needs_login) {
    return <LoginScreen />;
  }

  return (
    <div className="app">
      <Sidebar
        current={page}
        onChange={setPage}
        brokenPlatforms={status?.circuit_broken_platforms || []}
        loggedIn={status?.logged_in_platforms || []}
        enabled={status?.enabled_platforms || []}
        activeRounds={status?.active_rounds}
        statusLoading={!status}
      />
      <div className="main">
        <Topbar label={currentNav?.label ?? ""} onNavigate={setPage} />
        {!auth.configured && (
          <div className="security-warning" role="alert">
            <b>未设置访问口令</b>
            <span>接口目前没有访问保护，公网部署前请先设置口令。</span>
            <button type="button" className="btn sm" onClick={() => setPage("settings")}>去设置</button>
          </div>
        )}
        <main className="content" id="main-content">
          <PageBoundary key={page}>{render()}</PageBoundary>
        </main>
      </div>
    </div>
  );
}

// ===== 登录页：设置访问口令后，页面先登录；接口统一校验（P0）=====
function LoginScreen() {
  const { refreshAuth, showToast } = useApp();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.login(password);
      await refreshAuth();
    } catch (err) {
      setError((err as Error).message || "登录失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">IterWave</div>
        <h1>输入访问口令</h1>
        <p>这台服务器已开启访问保护，登录后才能查看账号和内容数据。</p>
        <label className="sr-only" htmlFor="accessPassword">访问口令</label>
        <input
          id="accessPassword" className="field" type="password" autoFocus
          value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="访问口令" autoComplete="current-password"
        />
        {error && <div className="login-error">{error}</div>}
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>
        <button type="button" className="btn sm soft" onClick={() => showToast("口令在服务器 config/.env 里设置；也可以在能打开页面时到「设置 → 通知与访问」修改", "info")}>
          忘记口令？
        </button>
      </form>
    </div>
  );
}

// ===== 全局顶部栏（对齐 demo：64px 半透明 + 日期 / 引导 / 通知 / 头像） =====
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
function todayLabel() {
  const d = new Date();
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 · ${WEEKDAYS[d.getDay()]}`;
}

function Topbar({ label, onNavigate }: { label: string; onNavigate: (p: string) => void }) {
  const { status, showToast } = useApp();
  const broken = status?.circuit_broken_platforms || [];
  const noLogin = (status?.enabled_platforms || []).filter((p) => !(status?.logged_in_platforms || []).includes(p));
  const notices = broken.length + noLogin.length;

  const openNotices = () => {
    if (!notices) { showToast("暂无需要处理的提醒", "info"); return; }
    const parts: string[] = [];
    if (broken.length) parts.push(`${broken.map((p) => platformMeta(p).label).join("、")} 处于熔断`);
    if (noLogin.length) parts.push(`${noLogin.map((p) => platformMeta(p).label).join("、")} 未登录`);
    showToast(parts.join("；"), broken.length ? "error" : "info");
  };

  return (
    <header className="topbar">
      <div className="crumb">
        IterWave <span className="crumb-sep" aria-hidden="true">/</span><strong>{label}</strong>
      </div>
      <div className="top-actions">
        <label className="sr-only" htmlFor="globalSearch">全局搜索</label>
        <input id="globalSearch" type="search" className="search" placeholder="搜索选题、内容、热点、任务…" />
        <span className="today">{todayLabel()}</span>
        <button type="button" className="btn sm soft" onClick={() => onNavigate("onboarding")}>
          <span aria-hidden="true">☷</span> 新手引导 / 快速配置
        </button>
        <button type="button" className="btn sm" onClick={openNotices} aria-label={`通知，${notices} 条待处理`}>
          <span aria-hidden="true">♧</span> 通知 <span>{notices}</span>
        </button>
        <div className="avatar" aria-hidden="true">I</div>
      </div>
    </header>
  );
}

// ===== 页面级错误边界：单页渲染异常时给出提示，不让整站空白 =====
class PageBoundary extends Component<{ children: ReactNode }, { err: Error | null }> {
  state = { err: null as Error | null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  render() {
    if (this.state.err) {
      return (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">!</span>页面渲染出错</div>
              <div className="panel-sub">
                通常是前端与后端版本不一致（后端改了接口没重启）或某条数据异常
              </div>
            </div>
          </div>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "var(--muted)" }}>
            {this.state.err.message}
          </pre>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button className="btn sm" onClick={() => this.setState({ err: null })}>重试</button>
            <button className="btn sm" onClick={() => { window.location.search = "?page=dashboard"; }}>
              回到总览
            </button>
            <button className="btn sm" onClick={() => window.location.reload()}>刷新页面</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <AppProvider>
      <Router />
    </AppProvider>
  );
}
