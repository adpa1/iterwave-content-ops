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
import GlobalSearch from "./components/GlobalSearch";

function Router() {
  // 初始读 URL param，之后 onChange 同步写 URL（demo 风格）
  const getInitial = () => {
    const m = new URLSearchParams(window.location.search).get("page");
    if (m && NAV_ITEMS.some((n) => n.page === m)) return m;
    return "dashboard";
  };
  const [page, setPageState] = useState<string>(getInitial);
  const { status, auth } = useApp();

  const setPage = (p: string, tab?: string, focus?: { roundId?: number; contentId?: string }) => {
    const url = new URL(window.location.href);
    url.searchParams.set("page", p);
    if (tab) url.searchParams.set("tab", tab);
    else url.searchParams.delete("tab");
    if (focus?.roundId != null) url.searchParams.set("focusRound", String(focus.roundId));
    else url.searchParams.delete("focusRound");
    if (focus?.contentId) url.searchParams.set("focusContent", focus.contentId);
    else url.searchParams.delete("focusContent");
    window.history.pushState({}, "", url.toString());
    setPageState(p);
    // 让页面内部的 useUrlTab 同步读取深链 tab（例如直接进入内容审核）。
    window.dispatchEvent(new PopStateEvent("popstate"));
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
  if (!auth.configured) {
    return <InitialSetupScreen />;
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

// ===== 首次启动：设置口令前禁止访问业务 API =====
function InitialSetupScreen() {
  const { refreshAuth } = useApp();
  const [password, setPassword] = useState("");
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password.trim().length < 6 || busy) {
      if (password.trim().length < 6) setError("访问口令至少 6 个字符");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.setPassword(password, bootstrapToken);
      setPassword("");
      setBootstrapToken("");
      await refreshAuth();
    } catch (err) {
      setError((err as Error).message || "设置失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">IterWave</div>
        <h1>首次安全设置</h1>
        <p>设置访问口令后才能访问账号、内容和运营数据。公网域名或反向代理部署需要服务器预先配置 ITERWAVE_BOOTSTRAP_TOKEN，并在下方输入对应引导口令；本机直连可留空。</p>
        <label className="sr-only" htmlFor="initialAccessPassword">新访问口令</label>
        <input
          id="initialAccessPassword" className="field" type="password" autoFocus
          value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="设置访问口令（至少 6 位）" autoComplete="new-password"
        />
        <label className="sr-only" htmlFor="initialBootstrapToken">首次设置引导口令</label>
        <input
          id="initialBootstrapToken" className="field" type="password"
          value={bootstrapToken} onChange={(e) => setBootstrapToken(e.target.value)}
          placeholder="服务器引导口令（公网部署必填）" autoComplete="off"
        />
        {error && <div className="login-error" role="alert">{error}</div>}
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "设置中…" : "设置访问口令"}
        </button>
      </form>
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

// ===== 全局顶部栏：全局搜索、引导与可操作的通知 =====
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
function todayLabel() {
  const d = new Date();
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 · ${WEEKDAYS[d.getDay()]}`;
}

function Topbar({ label, onNavigate }: { label: string; onNavigate: (p: string, tab?: string, focus?: { roundId?: number; contentId?: string }) => void }) {
  const { status } = useApp();
  const [noticesOpen, setNoticesOpen] = useState(false);
  const broken = status?.circuit_broken_platforms || [];
  const enabled = status?.enabled_platforms || [];
  const loggedIn = status?.logged_in_platforms || [];
  const noLogin = enabled.filter((p) => !loggedIn.includes(p));
  const noticeCount = broken.length + noLogin.filter((p) => !broken.includes(p)).length;

  useEffect(() => {
    if (!noticesOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNoticesOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [noticesOpen]);

  const go = (page: string, tab?: string, focus?: { roundId?: number; contentId?: string }) => {
    setNoticesOpen(false);
    onNavigate(page, tab, focus);
  };

  return (
    <header className="topbar">
      <div className="crumb">
        IterWave <span className="crumb-sep" aria-hidden="true">/</span><strong>{label}</strong>
      </div>
      <div className="top-actions">
        <GlobalSearch onNavigate={go} />
        <span className="today">{todayLabel()}</span>
        <button type="button" className="btn sm soft" onClick={() => go("onboarding")}>
          <span aria-hidden="true">☷</span> 新手引导 / 快速配置
        </button>
        <div className="notice-wrap">
          <button
            type="button"
            className="btn sm"
            onClick={() => setNoticesOpen((open) => !open)}
            aria-expanded={noticesOpen}
            aria-controls="notice-popover"
            aria-label={`通知，${noticeCount} 条待处理`}
          >
            <span aria-hidden="true">♧</span> 通知 <span className={noticeCount ? "notice-count active" : "notice-count"}>{noticeCount}</span>
          </button>
          {noticesOpen && (
            <div className="notice-popover" id="notice-popover" role="region" aria-label="需要处理的提醒">
              <div className="notice-popover-head">
                <strong>需要处理</strong>
                <span>{noticeCount} 项</span>
              </div>
              {broken.length === 0 && noLogin.length === 0 ? (
                <div className="notice-empty">暂无账号告警或登录问题。</div>
              ) : (
                <div className="notice-list">
                  {broken.map((p) => (
                    <button type="button" className="notice-item" key={`broken-${p}`} onClick={() => go("operations")}>
                      <span className="notice-item-mark danger" aria-hidden="true">!</span>
                      <span><b>{platformMeta(p).label}已触发熔断</b><small>进入运营控制台检查原因并恢复</small></span>
                      <span aria-hidden="true">→</span>
                    </button>
                  ))}
                  {noLogin.filter((p) => !broken.includes(p)).map((p) => (
                    <button type="button" className="notice-item" key={`login-${p}`} onClick={() => go("accounts")}>
                      <span className="notice-item-mark warning" aria-hidden="true">!</span>
                      <span><b>{platformMeta(p).label}尚未登录</b><small>扫码登录后才能执行平台操作</small></span>
                      <span aria-hidden="true">→</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
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
