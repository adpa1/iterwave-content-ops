import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { api, AuthStatus, SystemStatus, Platform } from "./api";

interface Toast { id: number; text: string; type?: "info" | "success" | "error"; }

interface AppContextType {
  status: SystemStatus | null;
  refreshStatus: () => Promise<void>;
  /** 访问保护状态：null = 检测中（首屏不闪「未登录」） */
  auth: AuthStatus | null;
  refreshAuth: () => Promise<void>;
  toasts: Toast[];
  showToast: (text: string, type?: Toast["type"]) => void;
  /** G-11：进行中的操作集合（key 由调用方定义，如 "publish:12" / "run:3"） */
  pending: Record<string, boolean>;
  /** G-11：包一层 pending 标记 + 防重复触发，返回 fn 的返回值；同一 key 执行中再次调用直接返回 undefined */
  withPending: <T>(key: string, fn: () => Promise<T>) => Promise<T | undefined>;
  isPending: (key: string) => boolean;
}

const AppContext = createContext<AppContextType | null>(null);

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("AppContext missing");
  return ctx;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const refreshAuth = useCallback(async () => {
    try {
      setAuth(await api.authStatus());
    } catch (e) {
      console.warn("auth status fetch failed:", e);
      setAuth({ configured: true, authenticated: false, needs_login: true });
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.status();
      setStatus(s);
    } catch (e) {
      console.warn("status fetch failed:", e);
    }
  }, []);

  const showToast = useCallback((text: string, type: Toast["type"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, text, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 3500);
  }, []);

  // 用 ref 做同步的"是否已在执行"判断：setState 更新是异步的，不能靠它去重
  const inflight = useRef<Set<string>>(new Set());

  const isPending = useCallback((key: string) => pending[key] === true, [pending]);

  const withPending = useCallback(async <T,>(key: string, fn: () => Promise<T>): Promise<T | undefined> => {
    if (inflight.current.has(key)) return undefined;
    inflight.current.add(key);
    setPending((prev) => ({ ...prev, [key]: true }));
    try {
      return await fn();
    } finally {
      inflight.current.delete(key);
      setPending((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  useEffect(() => {
    if (!auth?.authenticated) return;
    refreshStatus();
    const t = setInterval(refreshStatus, 15000);
    return () => clearInterval(t);
  }, [auth?.authenticated, refreshStatus]);

  return (
    <AppContext.Provider value={{ status, refreshStatus, auth, refreshAuth, toasts, showToast, pending, withPending, isPending }}>
      {children}
      <div className="toast-area" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.type || "info"}`}>
            <b>{t.type === "error" ? "需要处理" : t.type === "success" ? "已完成" : "提示"}</b>
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </AppContext.Provider>
  );
}
