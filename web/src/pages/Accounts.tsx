import { useEffect, useState } from "react";
import { api, Platform, AccountInfo, LoginStatus } from "../api";

const PLATFORM_EMOJI: Record<Platform, string> = {
  xhs: "小", douyin: "抖", kuaishou: "快", wechat_channels: "视",
};

const LOGIN_STATE_LABEL: Record<string, { label: string; color: string }> = {
  starting:       { label: "启动浏览器...", color: "gray" },
  qr_ready:       { label: "等待扫码",    color: "blue" },
  window_login:   { label: "等待扫码",    color: "blue" },  // Easel headed 模式
  scanned:        { label: "已扫描，等待跳转", color: "orange" },
  sms_required:   { label: "需短信验证",  color: "orange" },
  verifying:      { label: "保存登录态",  color: "blue" },
  success:        { label: "登录成功 ✓",  color: "green" },
  expired:        { label: "二维码过期",  color: "gray" },
  error:          { label: "登录失败 ✕",  color: "red" },
  unknown:        { label: "未知",        color: "gray" },
};

const ACCOUNT_STATUS_LABEL: Record<string, { label: string; color: string }> = {
  logged_in:     { label: "✓ 已登录",      color: "green" },
  logging_in:    { label: "▣ 登录中...",  color: "blue" },
  not_logged_in: { label: "✕ 未登录",      color: "gray" },
};

export default function Accounts() {
  const [accounts, setAccounts] = useState<AccountInfo[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [activeLogin, setActiveLogin] = useState<Platform | null>(null);
  const [loginStatus, setLoginStatus] = useState<LoginStatus | null>(null);
  const [smsCode, setSmsCode] = useState("");
  const [smsSubmitting, setSmsSubmitting] = useState(false);

  const load = async () => {
    setErr(null);
    try {
      const r = await api.accounts();
      setAccounts(r.accounts);
    } catch (e: any) { setErr(e.message); }
  };

  useEffect(() => { load(); }, []);

  // 轮询登录状态（2s 一次）。只有 success / expired / error 是终态——
  // unknown 只会出现在 runner 刚启动、状态文件还没写出时，不能当终态停止轮询（否则二维码永远出不来）
  useEffect(() => {
    if (!activeLogin) return;
    let stop = false;
    const deadline = Date.now() + 300_000;
    const t = setInterval(async () => {
      if (Date.now() > deadline) { clearInterval(t); return; }
      try {
        const st = await api.loginStatus(activeLogin);
        setLoginStatus(st);
        if (["success", "expired", "error"].includes(st.state)) {
          // 结束状态：再等 1s 后关弹窗
          setTimeout(() => {
            if (st.state === "success") {
              setActiveLogin(null);
              load();
            } else {
              // 失败/过期：保持弹窗让用户点"关闭"或"重试"
            }
          }, 1500);
          stop = true;
        }
      } catch (e) { /* ignore */ }
      if (stop) clearInterval(t);
    }, 2000);
    return () => clearInterval(t);
  }, [activeLogin]);

  const handleLogin = async (p: Platform) => {
    setActiveLogin(p);
    setLoginStatus(null);
    try {
      // 重新获取二维码 / 重试时必须重启 runner，否则后端会直接返回旧进程的状态
      const st = await api.startLogin(p, true);
      setLoginStatus(st);
    } catch (e: any) {
      setLoginStatus({ platform: p, platform_display: PLATFORM_EMOJI[p], state: "error", message: e.message, ts: Date.now() });
    }
  };

  const handleSubmitSms = async () => {
    if (!activeLogin) return;
    setSmsSubmitting(true);
    try {
      await api.submitLoginSms(activeLogin, smsCode);
      setSmsCode("");
    } catch (e: any) {
      alert("验证码提交失败：" + e.message);
    } finally {
      setSmsSubmitting(false);
    }
  };

  const handleLogout = async (p: Platform) => {
    if (!confirm(`确定要退出 ${PLATFORM_EMOJI[p]} 的登录态吗？本地登录信息将被删除，下次存草稿需要重新扫码。`)) return;
    try {
      await api.logout(p);
      load();
    } catch (e: any) { alert("退出失败：" + e.message); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          
          <h1 className="page-title">账号与登录</h1>
          <div className="page-desc">
            各平台可以登录不同定位的账号；在本页用手机 App 扫码登录，退出会清除本机保存的登录信息。账号定位在「目标与策略 → 账号定位与画像」设置。
          </div>
        </div>
      </div>

      {err && <div className="note" style={{ color: "var(--red)" }}>加载失败：{err}</div>}

      {/* 四平台账号状态卡 */}
      <div className="grid two" style={{ marginBottom: 20 }}>
        {(accounts || []).map((a) => {
          const st = ACCOUNT_STATUS_LABEL[a.status] || ACCOUNT_STATUS_LABEL.not_logged_in;
          const loginSt = a.login_proc_state ? LOGIN_STATE_LABEL[a.login_proc_state] : null;
          return (
            <div key={a.platform} className="panel" style={{ padding: 18 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontSize: 28 }}>{PLATFORM_EMOJI[a.platform as Platform]}</span>
                  <div>
                    <div style={{ fontWeight: 700, fontSize: 15 }}>{a.platform_display}</div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>本机登录</div>
                  </div>
                </div>
                <span className={`tag ${st.color}`}>{st.label}</span>
              </div>

              {/* 登录进程状态 */}
              {loginSt && a.status === "logging_in" && (
                <div style={{ marginBottom: 10 }}>
                  <span className={`tag ${loginSt.color}`} style={{ marginRight: 8 }}>▣ {loginSt.label}</span>
                  {a.login_proc_message && <span style={{ fontSize: 11, color: "var(--muted)" }}>{a.login_proc_message}</span>}
                </div>
              )}

              {/* 已登录态信息 */}
              {a.status === "logged_in" && (
                <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 10 }}>
                  ✓ 登录有效 · 可用于存草稿、运营与数据采集
                </div>
              )}

              {/* 操作按钮 */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {a.status === "not_logged_in" && (
                  <button className="btn primary" onClick={() => handleLogin(a.platform as Platform)}>▣ 扫码登录</button>
                )}
                {a.status === "logged_in" && (
                  <button className="btn" onClick={() => handleLogin(a.platform as Platform)}>↻ 重新扫码</button>
                )}
                {a.status === "logging_in" && (
                  <button className="btn" onClick={() => { api.cancelLogin(a.platform as Platform).then(load); setActiveLogin(null); }}>
                    ✕ 取消登录
                  </button>
                )}
                {a.status === "logged_in" && (
                  <button className="btn danger" onClick={() => handleLogout(a.platform as Platform)}>✕ 退出登录</button>
                )}
              </div>
            </div>
          );
        })}
        {accounts && accounts.length === 0 && (
          <div className="panel" style={{ gridColumn: "1/-1" }}>
            <div className="empty">暂无账号数据</div>
          </div>
        )}
      </div>

      {/* 说明卡 */}
      <div className="panel" style={{ padding: 14, fontSize: 12, color: "var(--muted)" }}>
        <strong style={{ color: "var(--text)" }}>扫码登录流程</strong>
        <br />
        1. 点「扫码登录」，本机会弹出平台登录页并显示二维码
        <br />
        2. 用手机 App 扫码，登录成功后自动保存登录信息
        <br />
        3. 之后存草稿、运营和数据采集都会自动使用该登录，不再弹登录页
        <br />
        <br />
        <strong style={{ color: "var(--orange)" }}>注意</strong>
        <br />
        · 登录一般 1–3 个月过期，过期后存草稿会失败，点「重新扫码」即可
        <br />
        · 平台登录页改版时二维码可能识别失败，请联系维护者更新
        <br />
        · 登录信息只保存在本机，不会上传或显示在页面上
      </div>

      {/* —— 扫码登录弹窗 —— */}
      {activeLogin && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)", zIndex: 9999,
          display: "flex", alignItems: "center", justifyContent: "center",
        }} onClick={() => {
          if (loginStatus && ["success", "expired", "error"].includes(loginStatus.state)) {
            setActiveLogin(null);
            load();
          }
        }}>
          <div
            style={{
              background: "#fff", borderRadius: 14, padding: 28, minWidth: 380, maxWidth: 440,
              boxShadow: "0 20px 60px rgba(0,0,0,0.3)", textAlign: "center",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ fontSize: 22, fontWeight: 700, marginBottom: 4 }}>
              {PLATFORM_EMOJI[activeLogin]} {loginStatus?.platform_display || "登录中"}
            </div>
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 16 }}>
              {loginStatus?.message || "准备中..."}
            </div>

            {/* QR 图片 */}
            {loginStatus?.qr_base64 && (
              <div style={{
                padding: 14, border: "1px solid var(--border)", borderRadius: 10,
                background: "#fff", display: "inline-block", marginBottom: 14,
              }}>
                <img src={loginStatus.qr_base64} alt="扫码登录 QR"
                  style={{ width: 220, height: 220, objectFit: "contain" }} />
              </div>
            )}
            {!loginStatus?.qr_base64 && loginStatus?.state === "starting" && (
              <div style={{
                width: 220, height: 220, border: "2px dashed var(--border)", borderRadius: 10,
                margin: "0 auto 14px", display: "flex", alignItems: "center", justifyContent: "center",
                color: "var(--muted)", fontSize: 12,
              }}>
                <div style={{ textAlign: "center" }}>
                  <div className="spinner" style={{ margin: "0 auto 8px" }} />
                  正在启动浏览器…
                  <br />请稍候
                </div>
              </div>
            )}

            {/* 状态指示 */}
            {loginStatus && (
              <div style={{ marginBottom: 14 }}>
                <span className={`tag ${LOGIN_STATE_LABEL[loginStatus.state]?.color || "gray"}`}>
                  {LOGIN_STATE_LABEL[loginStatus.state]?.label || loginStatus.state}
                </span>
                {loginStatus.state === "qr_ready" && (
                  <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>
                    用 {loginStatus.platform_display} App 扫描上方二维码（有效期约几分钟，过期会自动刷新）
                  </div>
                )}
                {loginStatus.state === "sms_required" && (
                  <form
                    style={{ display: "flex", gap: 6, justifyContent: "center", marginTop: 10 }}
                    onSubmit={(e) => { e.preventDefault(); handleSubmitSms(); }}
                  >
                    <label className="sr-only" htmlFor="sms-code">短信验证码</label>
                    <input
                      id="sms-code" className="field" inputMode="numeric" autoComplete="one-time-code"
                      placeholder="手机收到的验证码" value={smsCode} maxLength={8}
                      onChange={(e) => setSmsCode(e.target.value.replace(/\D/g, ""))}
                      style={{ width: 160 }}
                    />
                    <button type="submit" className="btn sm primary" disabled={smsSubmitting || smsCode.length < 4}>
                      {smsSubmitting ? "提交中…" : "提交"}
                    </button>
                  </form>
                )}
                {loginStatus.state === "expired" && (
                  <button className="btn sm primary" style={{ marginTop: 10 }} onClick={() => handleLogin(activeLogin)}>↻ 刷新二维码</button>
                )}
                {loginStatus.state === "error" && (
                  <button className="btn sm primary" style={{ marginTop: 10 }} onClick={() => handleLogin(activeLogin)}>↻ 重试</button>
                )}
                {loginStatus.state === "success" && (
                  <div className="tag green" style={{ marginTop: 6 }}>登录成功，登录态已保存</div>
                )}
              </div>
            )}

            {/* 关闭按钮 */}
            <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
              <button
                className="btn sm"
                onClick={() => {
                  api.cancelLogin(activeLogin).catch(() => {});
                  setActiveLogin(null); load();
                }}
              >关闭</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
