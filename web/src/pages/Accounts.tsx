import { useEffect, useState } from "react";
import { api, Platform, AccountInfo, LoginStatus } from "../api";

const PLATFORM_EMOJI: Record<Platform, string> = {
  xhs: "📕", douyin: "🎵", kuaishou: "⚡", wechat_channels: "💚",
};

const LOGIN_STATE_LABEL: Record<string, { label: string; color: string }> = {
  starting:       { label: "启动浏览器...", color: "gray" },
  qr_ready:       { label: "等待扫码",    color: "blue" },
  window_login:   { label: "等待扫码",    color: "blue" },  // Easel headed 模式
  scanned:        { label: "已扫描，等待跳转", color: "orange" },
  sms_required:   { label: "需短信验证",  color: "orange" },
  verifying:      { label: "保存登录态",  color: "blue" },
  success:        { label: "登录成功 ✅",  color: "green" },
  expired:        { label: "二维码过期",  color: "gray" },
  error:          { label: "登录失败 ❌",  color: "red" },
  unknown:        { label: "未知",        color: "gray" },
};

const ACCOUNT_STATUS_LABEL: Record<string, { label: string; color: string }> = {
  logged_in:     { label: "✅ 已登录",      color: "green" },
  logging_in:    { label: "🔐 登录中...",  color: "blue" },
  not_logged_in: { label: "❌ 未登录",      color: "gray" },
};

export default function Accounts() {
  const [accounts, setAccounts] = useState<AccountInfo[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [activeLogin, setActiveLogin] = useState<Platform | null>(null);
  const [loginStatus, setLoginStatus] = useState<LoginStatus | null>(null);

  const load = async () => {
    setErr(null);
    try {
      const r = await api.accounts();
      setAccounts(r.accounts);
    } catch (e: any) { setErr(e.message); }
  };

  useEffect(() => { load(); }, []);

  // 轮询登录状态（2s 一次，最多 300s）
  useEffect(() => {
    if (!activeLogin) return;
    let stop = false;
    const t = setInterval(async () => {
      try {
        const st = await api.loginStatus(activeLogin);
        setLoginStatus(st);
        if (["success", "expired", "error", "unknown"].includes(st.state)) {
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
      await api.startLogin(p, 300);
      // 初始状态从 accounts 里可能已有 QR，但还是主动拉一次 status
      const st = await api.loginStatus(p);
      setLoginStatus(st);
    } catch (e: any) {
      setLoginStatus({ platform: p, platform_display: PLATFORM_EMOJI[p], state: "error", message: e.message, ts: Date.now() });
    }
  };

  const handleLogout = async (p: Platform) => {
    if (!confirm(`确定要退出 ${PLATFORM_EMOJI[p]} 的登录态吗？本地 storage_state.json 将被删除，下次存草稿会提示重新扫码。`)) return;
    try {
      await api.logout(p);
      load();
    } catch (e: any) { alert("退出失败：" + e.message); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">ACCOUNT MANAGEMENT</div>
          <h1 className="page-title">账号与登录态管理</h1>
          <div className="page-desc">
            四平台登录状态 · 扫码登录（headed Chromium 不阻塞 FastAPI） · 注销清理 storage_state。
            <span style={{ marginLeft: 8, color: "var(--orange)", fontSize: 12 }}>
              ⚠ 扫码登录需要本机 GUI + Chromium；服务器（无显示）用 {`Xvfb + --headed`} 或在本地 Mac 跑
            </span>
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
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>storage_state.json</div>
                  </div>
                </div>
                <span className={`tag ${st.color}`}>{st.label}</span>
              </div>

              {/* 登录进程状态 */}
              {loginSt && a.status === "logging_in" && (
                <div style={{ marginBottom: 10 }}>
                  <span className={`tag ${loginSt.color}`} style={{ marginRight: 8 }}>🔐 {loginSt.label}</span>
                  {a.login_proc_message && <span style={{ fontSize: 11, color: "var(--muted)" }}>{a.login_proc_message}</span>}
                </div>
              )}

              {/* 已登录态信息 */}
              {a.status === "logged_in" && (
                <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 10 }}>
                  ✅ storage_state.json 存在 · Cookie 已持久化 · 可用于存草稿/运营/采集
                </div>
              )}

              {/* 操作按钮 */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {a.status === "not_logged_in" && (
                  <button className="btn primary" onClick={() => handleLogin(a.platform as Platform)}>🔐 扫码登录</button>
                )}
                {a.status === "logged_in" && (
                  <button className="btn" onClick={() => handleLogin(a.platform as Platform)}>🔄 重新扫码</button>
                )}
                {a.status === "logging_in" && (
                  <button className="btn" onClick={() => { api.cancelLogin(a.platform as Platform).then(load); setActiveLogin(null); }}>
                    ❌ 取消登录
                  </button>
                )}
                {a.status === "logged_in" && (
                  <button className="btn danger" onClick={() => handleLogout(a.platform as Platform)}>🗑 退出登录</button>
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
        <strong style={{ color: "var(--text)" }}>🔐 扫码登录流程：</strong>
        <br />
        1. 点「扫码登录」→ 本机 Chromium headed 浏览器弹出四平台 passport 登录页 → Playwright 自动识别并截图 QR
        <br />
        2. 用手机 App 扫描 QR → Worker 检测到离开登录页 → 自动保存 storage_state.json（chmod 600）
        <br />
        3. 登录成功后，后续存草稿/运营/采集动作都会自动恢复登录态，不再弹登录页
        <br />
        <br />
        <strong style={{ color: "var(--orange)" }}>⚠ 注意：</strong>
        <br />
        · 四平台 passport URL 可能随时改版，QR selector 需保持更新（Easel account_stats.py 有兜底）
        <br />
        · 登录态会过期（一般 1-3 个月），过期后存草稿会被踢回登录页 → 点「重新扫码」
        <br />
        · R23 安全：storage_state.json chmod 600，目录 700，git ignore，不进前端明文返回
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
                    请在弹出的 Chromium 浏览器中扫码（5 分钟内有效）
                  </div>
                )}
                {loginStatus.state === "expired" && (
                  <button className="btn sm primary" style={{ marginTop: 10 }} onClick={() => handleLogin(activeLogin)}>🔄 刷新二维码</button>
                )}
                {loginStatus.state === "error" && (
                  <button className="btn sm primary" style={{ marginTop: 10 }} onClick={() => handleLogin(activeLogin)}>🔄 重试</button>
                )}
                {loginStatus.state === "success" && (
                  <div className="tag green" style={{ marginTop: 6 }}>✅ 登录成功！storage_state 已保存</div>
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
