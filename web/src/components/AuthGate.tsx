import { useEffect, useState, type ReactNode } from "react";
import { api, AUTH_EVENT, type AuthStatus } from "../api";

/**
 * 访问口令门：
 * - 设置了口令且未登录 → 全屏登录页（所有业务页面都不渲染，避免一堆 401 红条）
 * - 未设置口令 → 正常使用，顶部常驻警告条，可直接设置口令
 */
export default function AuthGate({ children }: { children: ReactNode }) {
  const [st, setSt] = useState<AuthStatus | null>(null);
  const [checkErr, setCheckErr] = useState<string | null>(null);

  const check = async () => {
    try {
      setSt(await api.authStatus());
      setCheckErr(null);
    } catch (e) {
      setCheckErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    check();
    const onAuth = () => setSt({ required: true, authed: false });
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);

  if (!st) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-title">IterWave</div>
          <div className="auth-sub">{checkErr ? `无法连接服务：${checkErr}` : "正在连接服务…"}</div>
          {checkErr && <button className="btn" onClick={check}>重新连接</button>}
        </div>
      </div>
    );
  }
  if (st.required && !st.authed) {
    return <LoginScreen onDone={check} />;
  }
  return (
    <>
      {!st.required && <OpenModeBanner onDone={check} />}
      {children}
    </>
  );
}

function LoginScreen({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.login(token.trim());
      onDone();
      window.location.reload();
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m.startsWith("429") ? "尝试次数过多，请 10 分钟后再试" : "口令不正确");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-screen">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-title">IterWave</div>
        <div className="auth-sub">请输入访问口令</div>
        <label className="sr-only" htmlFor="iwToken">访问口令</label>
        <input id="iwToken" type="password" autoFocus autoComplete="current-password"
          className="auth-input" value={token} onChange={(e) => setToken(e.target.value)} />
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary" type="submit" disabled={busy || !token.trim()}>
          {busy ? "登录中…" : "进入"}
        </button>
        <div className="auth-hint">忘记口令：在服务器的 config/.env 里修改 ITERWAVE_ACCESS_TOKEN 后重启服务。</div>
      </form>
    </div>
  );
}

function OpenModeBanner({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [token2, setToken2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (token.length < 8) { setErr("口令至少 8 位"); return; }
    if (token !== token2) { setErr("两次输入不一致"); return; }
    setBusy(true);
    setErr(null);
    try {
      await api.setupAuth(token);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message.replace(/^\d+\s*/, "") : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="open-banner" role="alert">
      <span><b>未设置访问口令：</b>知道这个地址的任何人都能操作你的平台账号和配置。部署到公网前请先设置。</span>
      {!open ? (
        <button className="btn sm" onClick={() => setOpen(true)}>设置访问口令</button>
      ) : (
        <span className="open-banner-form">
          <input type="password" placeholder="新口令（至少 8 位）" value={token}
            onChange={(e) => setToken(e.target.value)} autoComplete="new-password" />
          <input type="password" placeholder="再输入一次" value={token2}
            onChange={(e) => setToken2(e.target.value)} autoComplete="new-password" />
          <button className="btn sm primary" disabled={busy} onClick={save}>{busy ? "保存中…" : "保存"}</button>
          <button className="btn sm ghost" onClick={() => setOpen(false)}>取消</button>
          {err && <span className="auth-err">{err}</span>}
        </span>
      )}
    </div>
  );
}
