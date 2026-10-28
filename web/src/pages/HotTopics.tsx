import { useEffect, useState } from "react";
import { api, HotTopicsResponse, HotTopicItem, ScoredTopic, type SystemConfigView } from "../api";
import DemoBadge from "../components/DemoBadge";
import EmptyHint from "../components/EmptyHint";
import { useApp } from "../AppContext";

type TabKey = "all" | "urgent" | "unknown";

export default function HotTopics() {
  const { showToast, withPending, isPending } = useApp();
  const [tab, setTab] = useState<TabKey>("all");
  const [hot, setHot] = useState<HotTopicsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scoredError, setScoredError] = useState<string | null>(null);
  const [scored, setScored] = useState<{ mode: string; topics: ScoredTopic[] } | null>(null);
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
      showToast(r.imported ? "✅ 已转入选题库" : r.message ?? "选题库已存在", r.imported ? "success" : "info");
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
    api.scoreTopics(10).then(setScored).catch((e) => {
      setScoredError(e instanceof Error ? e.message : String(e));
    });
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
      showToast(`✅ 已保存 ${res.changed.length} 项门限，已按新门限重新筛选`, "success");
      await loadConfig();
      await loadHot();
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
    }
  }

  const items = hot?.items || [];
  const filtered: HotTopicItem[] =
    tab === "all"
      ? items
      : tab === "urgent"
        ? items.filter((h) => h.time_sensitive === 1)
        : items.filter((h) => h.time_sensitive === null);

  const failedSources = (hot?.sources || []).filter((s) => s.status === "failed");

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">HOT TOPIC RADAR</div>
          <h1 className="page-title">热点雷达</h1>
          <div className="page-desc">
            数据源 60s.viki.moe 公益 API，后台每 {hot?.refresh_interval_minutes ?? 30} 分钟采集入库并与账号标签匹配，打开页面不再实时抓取。
            4 道筛选门：🔥热度 / ⏰时效 / 🎯维度匹配（不可关）/ ✅可发布性（不可关）。
          </div>
        </div>
        <div className="head-actions">
          <DemoBadge level="no_request" text="公益 API 可能限流；采集失败时不显示热度数字" />
          <span className="tag teal">数据源 · 60s.viki.moe</span>
          {hot?.stale && !refreshing && <span className="tag orange">数据已过刷新间隔</span>}
          <button className="btn sm" onClick={triggerRefresh} disabled={refreshing}>
            {refreshing ? "后台采集中…" : "↻ 立即采集"}
          </button>
        </div>
      </div>

      {/* 数据源状态（R22：失败就明说，不给假数字） */}
      <div className="flex wrap" style={{ gap: 8, marginBottom: 16 }}>
        {(hot?.sources || []).map((s) => (
          <span key={s.platform} className={`tag ${s.status === "failed" ? "red" : "green"}`}>
            {s.platform} · {s.status === "failed" ? "采集失败" : s.status === "demo" ? "演示数据" : "真实采集"}
          </span>
        ))}
        {hot?.match && (
          <span className="tag blue">
            标签匹配 · 关联 {hot.match.matched} · 无关 {hot.match.rejected} · 待研判 {hot.match.pending}
            {hot.match.method === "keyword" ? "（未配置 LLM，仅关键词匹配）" : ""}
          </span>
        )}
        {hot?.refreshed_at && (
          <span className="tag gray" style={{ marginLeft: "auto" }}>
            最近更新 {hot.refreshed_at.replace("T", " ")}
          </span>
        )}
      </div>

      {failedSources.length > 0 && (
        <div className="panel" style={{ marginBottom: 16, borderColor: "var(--red)" }}>
          <div className="panel-sub">
            ⚠ {failedSources.length} 个数据源采集失败，对应平台不显示热度数字（R22）：
            {failedSources.map((s) => ` ${s.platform}`).join(",")}
          </div>
        </div>
      )}

      {/* G-24：4 道门限 —— 热度/时效可调，维度匹配/可发布性为 R22 硬锁不可关 */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">⛨</span>筛选门限</div>
            <div className="panel-sub">
              热度门与时效门可在线调整；🎯维度匹配与✅可发布性为 R22 安全底线，<b>无关闭入口且后端拒绝关闭请求</b>
            </div>
          </div>
          <div className="head-actions">
            <button className="btn primary sm" disabled={!cfgView || isPending("hot:gates")} onClick={saveGates}>
              {isPending("hot:gates") ? "保存中…" : "保存门限"}
            </button>
          </div>
        </div>
        {!cfgView ? (
          <div className="note">加载门限配置中…</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>门</th><th>当前门限</th><th>启用</th><th>说明</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>🔥 热度门槛</td>
                  <td>
                    <input className="field" type="number" min={0} value={gates.heat}
                      style={{ width: 120 }}
                      onChange={(e) => setGates((g) => ({ ...g, heat: e.target.value }))} />
                  </td>
                  <td>
                    <label><input type="checkbox" checked={gates.heatOn}
                      onChange={(e) => setGates((g) => ({ ...g, heatOn: e.target.checked }))} /> 启用</label>
                  </td>
                  <td className="metric-foot">低于该热度的热点不进候选（可关）</td>
                </tr>
                <tr>
                  <td>⏰ 时效门槛</td>
                  <td>
                    <input className="field" type="number" min={1} max={720} value={gates.hours}
                      style={{ width: 100 }}
                      onChange={(e) => setGates((g) => ({ ...g, hours: e.target.value }))} />
                    <span className="metric-foot"> 小时</span>
                  </td>
                  <td>
                    <label><input type="checkbox" checked={gates.timeOn}
                      onChange={(e) => setGates((g) => ({ ...g, timeOn: e.target.checked }))} /> 启用</label>
                  </td>
                  <td className="metric-foot">超出时效窗口的热点不进候选（可关）</td>
                </tr>
                <tr>
                  <td>🎯 维度匹配</td>
                  <td><span className="tag green">🔒 强制开启</span></td>
                  <td><span className="tag gray">无开关</span></td>
                  <td className="metric-foot">R22 硬锁：关闭会让内容偏离账号定位</td>
                </tr>
                <tr>
                  <td>✅ 可发布性</td>
                  <td><span className="tag green">🔒 强制开启</span></td>
                  <td><span className="tag gray">无开关</span></td>
                  <td className="metric-foot">R22 硬锁：关闭会放出红线敏感词内容</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="tabs" style={{ marginBottom: 16 }}>
        {[
          { k: "all", l: `全部 (${items.length})` },
          { k: "urgent", l: "窗口短" },
          { k: "unknown", l: "时效未知" },
        ].map((t) => (
          <button key={t.k} className={`tab ${tab === t.k ? "active" : ""}`} onClick={() => setTab(t.k as TabKey)}>
            {t.l}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" />加载热点...</div>
      ) : error ? (
        <div className="panel"><div className="panel-sub" style={{ color: "var(--red)" }}>热点接口异常：{error}</div></div>
      ) : filtered.length === 0 ? (
        <div className="panel">
          {hot?.enabled === false ? (
            <EmptyHint next="到「系统设置」确认热点采集已开启，再点右上角「↻ 立即采集」">
              {hot.message || "热点采集已关闭"}
            </EmptyHint>
          ) : items.length === 0 ? (
            <EmptyHint next="点右上角「↻ 立即采集」立即采集热点">暂无通过筛选门的热点</EmptyHint>
          ) : (
            <EmptyHint next="切回「全部」查看已采集热点，或点右上角「↻ 立即采集」">该筛选下暂无热点</EmptyHint>
          )}
        </div>
      ) : (
        <div className="grid three" style={{ marginBottom: 20 }}>
          {filtered.slice(0, 12).map((h) => (
            <div key={h.id} className="panel" style={{ marginBottom: 0 }}>
              <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 6 }}>{h.title}</div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 10 }}>
                来源：{h.source}
              </div>
              <div className="flex between wrap">
                {h.heat !== null ? (
                  <span className="tag orange">🔥 {h.heat.toLocaleString()} {h.heat_unit}</span>
                ) : (
                  <span className="tag gray">热度未知</span>
                )}
                {h.time_sensitive === 1 && <span className="tag red">⏰ 时效窗口短</span>}
                {h.time_sensitive === null && <span className="tag gray">时效未知</span>}
              </div>
              <div className="flex wrap" style={{ marginTop: 10, gap: 6 }}>
                {h.dimension_match === true ? (
                  <span className="tag blue" title={h.match_reason ?? undefined}>
                    关联 {h.match_score != null ? Math.round(h.match_score) : ""}
                    {h.dimension ? ` · ${h.dimension}` : h.dimension_hits.length ? ` · ${h.dimension_hits.join("/")}` : ""}
                  </span>
                ) : h.dimension_match === false ? (
                  <span className="tag gray" title={h.match_reason ?? undefined}>
                    与定位无关{h.match_score != null ? ` · ${Math.round(h.match_score)}` : ""}
                  </span>
                ) : (
                  <span className="tag gray">待研判</span>
                )}
                {h.publishable ? (
                  <span className="tag green">✅ 可发布</span>
                ) : (
                  <span className="tag red">
                    {h.redline_hits.length ? `含红线：${h.redline_hits.join("/")}` : "落入禁止方向"}
                  </span>
                )}
              </div>
              {h.match_angle && (
                <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 8 }}>切入角度：{h.match_angle}</div>
              )}
              <div className="flex wrap" style={{ marginTop: 10, gap: 6 }}>
                {h.url ? (
                  <a className="btn sm" href={h.url} target="_blank" rel="noreferrer">查看原文</a>
                ) : (
                  <span className="tag gray">无原文链接（不能入库 · R22）</span>
                )}
                <button
                  className="btn sm primary"
                  disabled={!h.url || importingKey === `${h.source}|${h.title}`}
                  title={h.url ? "写入选题库，可被评分引擎评分" : "R22：缺少原文链接，禁止入库"}
                  onClick={() => importTopic(h)}
                >
                  {importingKey === `${h.source}|${h.title}` ? "入库中…" : "→ 转选题"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title"><span className="icon">▦</span>选题评分引擎实时输出</div>
            <div className="panel-sub">
              从本地选题库抽取 {scored?.topics.length || 10} 条，当前模式：
              <span className="tag blue" style={{ marginLeft: 6 }}>{scored?.mode || "-"}</span>
            </div>
          </div>
        </div>
        {!scored ? (
          <div className="loading"><div className="spinner" />加载评分...</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>排名</th><th>选题</th><th>维度</th><th>总分</th><th>定位</th><th>时效</th><th>新鲜度</th><th>探索</th>
                </tr>
              </thead>
              <tbody>
                {scored.topics.length === 0 && (
                  <tr><td colSpan={8}>
                    <EmptyHint className="" next="到「内容资产 → 选题库」确认选题库可读后回到本页">评分引擎暂无输出</EmptyHint>
                  </td></tr>
                )}
                {scored.topics.map((t, i) => (
                  <tr key={t.id}>
                    <td style={{ fontSize: 11, color: "var(--subtle)", fontWeight: 600 }}>#{i + 1}</td>
                    <td>{t.title}</td>
                    <td><span className="tag gray">{t.dimension}</span></td>
                    <td style={{ fontWeight: 700, color: "var(--blue)" }}>{t.total_score}</td>
                    <td style={{ fontSize: 11 }}>{t.breakdown.position_match?.raw?.toFixed(0) || "-"}</td>
                    <td style={{ fontSize: 11 }}>{t.breakdown.timeliness?.raw?.toFixed(0) || "-"}</td>
                    <td style={{ fontSize: 11 }}>{t.breakdown.freshness?.raw?.toFixed(0) || "-"}</td>
                    <td>{t.is_exploration ? <span className="tag purple">ε</span> : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
