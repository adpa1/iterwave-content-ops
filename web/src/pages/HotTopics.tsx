import { useEffect, useState } from "react";
import { api, HotTopicsResponse, HotTopicItem, type SystemConfigView } from "../api";
import { useUrlTab } from "../nav";
import { platformLabel } from "../platforms";
import DemoBadge from "../components/DemoBadge";
import EmptyHint from "../components/EmptyHint";
import { useApp } from "../AppContext";

type TabKey = "all" | "matched" | "urgent" | "unknown";
const PAGE_TABS = ["list", "logic"] as const;
type PageTab = typeof PAGE_TABS[number];

export default function HotTopics() {
  const { showToast, withPending, isPending } = useApp();
  const [tab, setTab] = useState<TabKey>("all");
  const [pageTab, setPageTab] = useUrlTab<PageTab>(PAGE_TABS, "list");
  const [keyword, setKeyword] = useState("");
  const [sortBy, setSortBy] = useState<"heat" | "match">("heat");
  const [limit, setLimit] = useState(20);
  const [hot, setHot] = useState<HotTopicsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // G-24：4 道门限（热度/时效可调；维度匹配/可发布性为 R22 硬锁，不可关）
  const [cfgView, setCfgView] = useState<SystemConfigView | null>(null);
  const [cfgError, setCfgError] = useState<string | null>(null);
  const [gates, setGates] = useState({ heat: "", heatOn: true, hours: "", timeOn: true });
  const [importingKey, setImportingKey] = useState<string | null>(null);

  /** R22：热点转选题必须带来源与原文链接，缺证据直接拒绝 */
  async function importTopic(h: HotTopicItem) {
    const key = `${h.source}|${h.title}`;
    setImportingKey(key);
    try {
      const r = await api.importHotTopic({
        title: h.title,
        source: h.source,
        url: h.url ?? "",
        dimension: h.dimension ?? h.dimension_hits?.[0],
      });
      showToast(r.imported ? "✓ 已转入选题库" : r.message ?? "选题库已存在", r.imported ? "success" : "info");
    } catch (e: any) {
      showToast(`入库失败: ${e?.message ?? e}`, "error");
    } finally {
      setImportingKey(null);
    }
  }

  async function loadHot() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.hotTopics();
      setHot(data);
      setRefreshing(!!data.refresh_job?.running);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  /** 手动刷新：只触发后台采集，页面轮询库内数据，直到后台任务结束 */
  async function triggerRefresh() {
    setRefreshing(true);
    try {
      await api.refreshHotTopics();
    } catch (e: any) {
      showToast(`触发采集失败: ${e?.message ?? e}`, "error");
      setRefreshing(false);
    }
  }

  useEffect(() => {
    if (!refreshing) return;
    const t = setInterval(async () => {
      try {
        const data = await api.hotTopics();
        setHot(data);
        if (!data.refresh_job?.running) {
          setRefreshing(false);
          if (data.refresh_job?.error) showToast(`采集失败: ${data.refresh_job.error}`, "error");
        }
      } catch { /* 下次轮询重试 */ }
    }, 3000);
    return () => clearInterval(t);
  }, [refreshing]);

  useEffect(() => {
    loadHot();
    loadConfig();
  }, []);

  /** G-24：读门限配置（可编辑两项 + R22 两道硬锁只读） */
  async function loadConfig() {
    setCfgError(null);
    try {
      const v = await api.config();
      setCfgView(v);
      setGates({
        heat: v.editable.hot_topics.heat_threshold == null ? "" : String(v.editable.hot_topics.heat_threshold),
        heatOn: v.editable.hot_topics.heat_filter_enabled ?? true,
        hours: v.editable.hot_topics.time_window_hours == null ? "" : String(v.editable.hot_topics.time_window_hours),
        timeOn: v.editable.hot_topics.time_filter_enabled ?? true,
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setCfgError(msg);  // N2 真实修复：可见错误态，不再静默 console.error
    }
  }

  /** 只提交差量；维度匹配/可发布性两门无开关，后端也会拒绝关闭（R22） */
  async function saveGates() {
    if (!cfgView) return;
    const ed = cfgView.editable.hot_topics;
    const updates: Record<string, unknown> = {};
    if (gates.heat !== "" && Number(gates.heat) !== ed.heat_threshold) {
      updates["hot_topics.filters.heat_threshold"] = Number(gates.heat);
    }
    if (gates.heatOn !== (ed.heat_filter_enabled ?? true)) {
      updates["hot_topics.filters.heat_filter_enabled"] = gates.heatOn;
    }
    if (gates.hours !== "" && Number(gates.hours) !== ed.time_window_hours) {
      updates["hot_topics.filters.time_window_hours"] = Number(gates.hours);
    }
    if (gates.timeOn !== (ed.time_filter_enabled ?? true)) {
      updates["hot_topics.filters.time_filter_enabled"] = gates.timeOn;
    }
    if (Object.keys(updates).length === 0) {
      showToast("没有需要保存的门限改动", "info");
      return;
    }
    try {
      const res = await withPending("hot:gates", () => api.patchConfig(updates));
      if (!res) return;
      showToast(`✓ 已保存 ${res.changed.length} 项门限，已按新门限重新筛选`, "success");
      await loadConfig();
      await loadHot();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  }

  const items = hot?.items || [];
  const kw = keyword.trim();
  const filtered: HotTopicItem[] = items
    .filter((h) =>
      tab === "all" ? true
        : tab === "matched" ? h.dimension_match === true
          : tab === "urgent" ? h.time_sensitive === 1
            : h.time_sensitive === null)
    .filter((h) => !kw || h.title.includes(kw))
    .slice()
    .sort((a, b) => sortBy === "match"
      ? (b.match_score ?? -1) - (a.match_score ?? -1)
      : (b.heat ?? -1) - (a.heat ?? -1));

  const sources = hot?.sources || [];
  const failedSources = sources.filter((s) => s.status === "failed");
  const matched = hot?.match?.matched ?? items.filter((h) => h.dimension_match === true).length;
  const pending = hot?.match?.pending ?? items.filter((h) => h.dimension_match === null).length;
  const urgent = items.filter((h) => h.time_sensitive === 1).length;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">DISCOVER & TREND RIDING</div>
          <h1 className="page-title">热点雷达</h1>
          <div className="page-desc">
            后台定时采集各平台热榜（{hot?.refresh_enabled === false ? "已关闭" : hot?.refresh_schedule ?? "按配置"}），先判断是否服务于账号定位，再决定转选题、观察或放弃。
          </div>
        </div>
        <div className="head-actions">
          {hot?.stale && !refreshing && <span className="tag orange">数据已过刷新间隔</span>}
          <button className="btn" onClick={() => setPageTab("logic")}>筛选逻辑</button>
          <button className="btn primary" onClick={triggerRefresh} disabled={refreshing}>
            {refreshing ? "后台采集中…" : "↻ 立即采集"}
          </button>
        </div>
      </div>

      <div className="grid kpis" style={{ marginBottom: 15 }}>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">本次采集</span><span className="metric-icon" aria-hidden="true">✳</span></div>
          <div className="metric-val">{items.length} <span className="metric-unit">条</span></div>
          <div className="metric-foot">{hot?.refreshed_at ? `更新于 ${hot.refreshed_at.replace("T", " ").slice(5, 16)}` : "尚未采集"}</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">与定位关联</span><span className="metric-icon" style={{ background: "var(--teal-weak)", color: "var(--teal)" }} aria-hidden="true">◎</span></div>
          <div className="metric-val">{matched} <span className="metric-unit">条</span></div>
          <div className="metric-foot">{pending > 0 ? `${pending} 条待研判` : "已全部研判"}{hot?.match?.method === "keyword" ? " · 仅关键词匹配" : ""}</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">窗口短</span><span className="metric-icon" style={{ background: "var(--orange-weak)", color: "var(--orange)" }} aria-hidden="true">◷</span></div>
          <div className="metric-val">{urgent} <span className="metric-unit">条</span></div>
          <div className="metric-foot">需要尽快决定是否跟进</div>
        </div>
        <div className="metric">
          <div className="metric-top"><span className="metric-label">数据源</span><span className="metric-icon" aria-hidden="true">⌁</span></div>
          <div className="metric-val">{sources.length - failedSources.length}<span className="metric-unit"> / {sources.length} 正常</span></div>
          <div className="metric-foot" style={failedSources.length ? { color: "var(--red)" } : undefined}>
            {failedSources.length ? `${failedSources.map((s) => platformLabel(s.platform)).join("、")} 采集失败，不显示热度` : "全部采集成功"}
          </div>
        </div>
      </div>

      <div className="tabs page-tabs">
        <button type="button" className={`tab ${pageTab === "list" ? "active" : ""}`} onClick={() => setPageTab("list")}>热点列表与筛选</button>
        <button type="button" className={`tab ${pageTab === "logic" ? "active" : ""}`} onClick={() => setPageTab("logic")}>筛选逻辑与数据源</button>
      </div>

      {pageTab === "logic" && (
      <>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⛨</span>四道筛选门</div>
            <div className="panel-sub">热度与时效门限可调整；维度匹配与可发布性是安全底线，不能关闭</div>
          </div>
          <div className="head-actions">
            <button className="btn primary sm" disabled={!cfgView || isPending("hot:gates")} onClick={saveGates}>
              {isPending("hot:gates") ? "保存中…" : "保存门限"}
            </button>
          </div>
        </div>
        {!cfgView ? (
          <div className="note">{cfgError ? `门限配置加载失败：${cfgError}` : "加载门限配置中…"}</div>
        ) : (
          <div className="grid four">
            <div className="gate-card">
              <div className="gate-no">01 · 热度</div>
              <div className="gate-title">热度够不够？</div>
              <div className="gate-desc">低于门槛的热点不进入候选</div>
              <div className="gate-ctrl">
                <input className="field" type="number" min={0} value={gates.heat}
                  onChange={(e) => setGates((g) => ({ ...g, heat: e.target.value }))} />
                <label className="gate-switch"><input type="checkbox" checked={gates.heatOn}
                  onChange={(e) => setGates((g) => ({ ...g, heatOn: e.target.checked }))} /> 启用</label>
              </div>
            </div>
            <div className="gate-card">
              <div className="gate-no">02 · 时效</div>
              <div className="gate-title">来不来得及做？</div>
              <div className="gate-desc">超出时效窗口的热点不进入候选</div>
              <div className="gate-ctrl">
                <input className="field" type="number" min={1} max={720} value={gates.hours}
                  onChange={(e) => setGates((g) => ({ ...g, hours: e.target.value }))} />
                <span className="metric-foot">小时</span>
                <label className="gate-switch"><input type="checkbox" checked={gates.timeOn}
                  onChange={(e) => setGates((g) => ({ ...g, timeOn: e.target.checked }))} /> 启用</label>
              </div>
            </div>
            <div className="gate-card">
              <div className="gate-no">03 · 维度匹配</div>
              <div className="gate-title">是否服务账号定位？</div>
              <div className="gate-desc">与定位无关的热点不会转成选题</div>
              <div className="gate-ctrl"><span className="tag green">强制开启</span></div>
            </div>
            <div className="gate-card">
              <div className="gate-no">04 · 可发布性</div>
              <div className="gate-title">能不能安全发布？</div>
              <div className="gate-desc">命中红线词或禁止方向的热点直接排除</div>
              <div className="gate-ctrl"><span className="tag green">强制开启</span></div>
            </div>
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⌁</span>数据源状态</div>
            <div className="panel-sub">同一平台同一标题只更新不新增；跨平台同一事件合并展示；热点保留 {hot?.storage?.retention_days ?? "-"} 天</div>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>平台</th><th>状态</th><th>说明</th></tr></thead>
            <tbody>
              {sources.length === 0 && <tr><td colSpan={3}><EmptyHint className="" next="点右上角「立即采集」">还没有采集记录</EmptyHint></td></tr>}
              {sources.map((s) => (
                <tr key={s.platform}>
                  <td>{platformLabel(s.platform)}</td>
                  <td><span className={`tag ${s.status === "failed" ? "red" : s.status === "demo" ? "orange" : "green"}`}>{s.status === "failed" ? "采集失败" : s.status === "demo" ? "演示数据" : "正常"}</span></td>
                  <td className="metric-foot">{s.status === "failed" ? (s.message || "公益接口可能限流，下次定时采集会自动重试") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {hot?.storage && <div className="metric-foot" style={{ marginTop: 10 }}>库内 {hot.storage.rows} 条，上限 {hot.storage.max_rows} 条</div>}
      </div>
      </>
      )}

      {pageTab === "list" && (
      <>
      <div className="filter-bar">
        <div className="tabs">
          {[
            { k: "all", l: `全部 ${items.length}` },
            { k: "matched", l: `与定位关联 ${matched}` },
            { k: "urgent", l: "窗口短" },
            { k: "unknown", l: "时效未知" },
          ].map((t) => (
            <button key={t.k} type="button" className={`tab ${tab === t.k ? "active" : ""}`} onClick={() => { setTab(t.k as TabKey); setLimit(20); }}>
              {t.l}
            </button>
          ))}
        </div>
        <div className="grow" />
        <input className="field" placeholder="搜索热点关键词" value={keyword} onChange={(e) => setKeyword(e.target.value)} />
        <select className="field" value={sortBy} onChange={(e) => setSortBy(e.target.value as "heat" | "match")}>
          <option value="heat">按热度</option>
          <option value="match">按定位关联度</option>
        </select>
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" />加载热点...</div>
      ) : error ? (
        <div className="panel"><div className="panel-sub" style={{ color: "var(--red)" }}>热点接口异常：{error}</div></div>
      ) : filtered.length === 0 ? (
        <div className="panel">
          {hot?.enabled === false ? (
            <EmptyHint next="到「系统设置」开启热点采集，再点右上角「立即采集」">
              {hot.message || "热点采集已关闭"}
            </EmptyHint>
          ) : items.length === 0 ? (
            <EmptyHint next="点右上角「立即采集」">暂无通过筛选门的热点</EmptyHint>
          ) : (
            <EmptyHint next="切回「全部」或清空搜索词">该筛选下暂无热点</EmptyHint>
          )}
        </div>
      ) : (
        <>
        <div className="hot-grid">
          {filtered.slice(0, limit).map((h, idx) => {
            const key = `${h.source}|${h.title}`;
            return (
            <div key={`${h.id}-${idx}`} className="hot-card">
              <div className="hot-top">
                <div className="flex wrap" style={{ gap: 6 }}>
                  {h.time_sensitive === 1 ? <span className="tag red">窗口短</span>
                    : h.time_sensitive === null ? <span className="tag gray">时效未知</span>
                    : <span className="tag green">可跟进</span>}
                  <span className="tag gray">{platformLabel(h.source.split("/").pop() || h.source)}</span>
                  {h.also_on && h.also_on.length > 0 && <span className="tag blue">多平台 · {h.also_on.length + 1}</span>}
                </div>
                <span className="metric-foot">{h.heat !== null ? `热度 ${h.heat.toLocaleString()}` : "热度未知"}</span>
              </div>
              <div className="hot-title">{h.title}</div>
              <div className="hot-meta">
                <span>定位关联 {h.match_score != null ? `${Math.round(h.match_score)}/100` : h.dimension_match === null ? "待研判" : "—"}</span>
                {h.dimension && <span>维度 {h.dimension}</span>}
                {h.fit_platforms && <span>适合账号：{h.fit_platforms.length ? h.fit_platforms.map((p) => platformLabel(p)).join(" / ") : "暂无匹配平台"}</span>}
                {h.also_on && h.also_on.length > 0 && <span>同时在榜：{h.also_on.map((o) => platformLabel(o.platform)).join(" / ")}</span>}
              </div>
              {(h.match_angle || h.match_reason || h.dimension_match === false || !h.publishable) && (
                <div className="hot-angle" style={{ marginTop: 10 }}>
                  {h.match_angle && <div><b>建议切入：</b>{h.match_angle}</div>}
                  {h.match_reason && <div style={{ color: "var(--muted)" }}>研判理由：{h.match_reason}</div>}
                  {h.dimension_match === false && !h.match_reason && <div style={{ color: "var(--muted)" }}>与账号定位无关</div>}
                  {!h.publishable && <div style={{ color: "var(--red)" }}>{h.redline_hits.length ? `含红线词：${h.redline_hits.join("、")}` : "落入禁止方向"}</div>}
                </div>
              )}
              <div className="hot-actions">
                <div className="flex wrap" style={{ gap: 6 }}>
                  {h.dimension_match === true ? <span className="tag blue">与定位关联</span>
                    : h.dimension_match === false ? <span className="tag gray">与定位无关</span>
                    : <span className="tag gray">待研判</span>}
                  {h.publishable ? <span className="tag green">可发布</span> : <span className="tag red">不可发布</span>}
                </div>
                <div className="flex" style={{ gap: 6 }}>
                  {h.url ? (
                    <a className="btn sm" href={h.url} target="_blank" rel="noreferrer">查看原文</a>
                  ) : (
                    <span className="tag gray">无原文链接</span>
                  )}
                  <button
                    className="btn sm primary"
                    disabled={!h.url || importingKey === key}
                    title={h.url ? "加入选题库，参与选题评分" : "缺少原文链接，不能加入选题库"}
                    onClick={() => importTopic(h)}
                  >
                    {importingKey === key ? "加入中…" : "加入选题库"}
                  </button>
                </div>
              </div>
            </div>
            );
          })}
        </div>
        {filtered.length > limit && (
          <div style={{ textAlign: "center", marginTop: 14 }}>
            <button className="btn" onClick={() => setLimit((n) => n + 20)}>再显示 20 条（剩余 {filtered.length - limit} 条）</button>
          </div>
        )}
        </>
      )}
      </>
      )}
    </>
  );
}
