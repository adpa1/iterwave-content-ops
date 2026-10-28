/**
 * G-12 · 统一异步面板三态
 *
 * 全站所有「拉数据再渲染」的面板都走这一层，避免每个页面各写一套
 * loading / error / empty，且错误必须带重试按钮（不能只显示一行红字）。
 *
 *   <AsyncPanel loading={loading} error={error} onRetry={load} empty={items.length === 0} emptyText="暂无数据">
 *     ...内容...
 *   </AsyncPanel>
 */
import React from "react";

interface AsyncPanelProps {
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** 数据为空。loading 为 true 时不生效（先显示 loading） */
  empty?: boolean;
  emptyText?: React.ReactNode;
  /** loading 时的文案 */
  loadingText?: string;
  /** 是否用 panel 外壳包裹三态（默认 true）；false 时只渲染裸内容 */
  boxed?: boolean;
  children: React.ReactNode;
}

export default function AsyncPanel({
  loading = false,
  error = null,
  onRetry,
  empty = false,
  emptyText = "暂无数据",
  loadingText = "加载中…",
  boxed = true,
  children,
}: AsyncPanelProps) {
  const wrap = (node: React.ReactNode) =>
    boxed ? <div className="panel">{node}</div> : <>{node}</>;

  if (loading) {
    return wrap(
      <div className="loading">
        <div className="spinner" />
        {loadingText}
      </div>,
    );
  }

  if (error) {
    return wrap(
      <div style={{ padding: 4 }}>
        <div style={{ color: "var(--red)", fontSize: 13, marginBottom: 10 }}>
          加载失败：{error}
        </div>
        {onRetry && (
          <button className="btn sm" onClick={onRetry}>
            ↻ 重试
          </button>
        )}
      </div>,
    );
  }

  if (empty) {
    return wrap(<div className="empty">{emptyText}</div>);
  }

  return <>{children}</>;
}
