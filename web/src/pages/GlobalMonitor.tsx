import { useEffect, useState } from "react";
import { api, GlobalMonitorResponse } from "../api";

const PLATFORM_LABELS: Record<string, string> = {
  xhs: "小红书", douyin: "抖音", kuaishou: "快手", wechat_channels: "视频号",
};

export default function GlobalMonitor() {
  const [days, setDays] = useState(30);
  const [tenantFilter, setTenantFilter] = useState("all");
  const [platformFilter, setPlatformFilter] = useState("all");
  const [data, setData] = useState<GlobalMonitorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async (windowDays = days) => {
    setLoading(true);
    setError("");
    try {
      setData(await api.globalMonitor(windowDays));
    } catch (e) {
      setError(e instanceof Error ? e.message : "全局监控数据加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(days); }, [days]);

  const visibleTenants = (data?.tenants ?? []).filter((tenant) => tenantFilter === "all" || tenant.tenant_id === tenantFilter);
  const total = data ? visibleTenants.reduce((sum, tenant) => ({
    tenants: sum.tenants + 1,
    available_tenants: sum.available_tenants + (tenant.db_available ? 1 : 0),
    contents_period: sum.contents_period + tenant.contents_period,
    contents_all: sum.contents_all + tenant.contents_all,
    drafts_all: sum.drafts_all + tenant.drafts_all,
    drafts_pending: sum.drafts_pending + tenant.drafts_pending,
    drafts_saved: sum.drafts_saved + tenant.drafts_saved,
    drafts_published: sum.drafts_published + tenant.drafts_published,
    drafts_failed: sum.drafts_failed + tenant.drafts_failed,
    media_active: sum.media_active + tenant.media_active,
    media_usage: sum.media_usage + tenant.media_usage,
  }), { tenants: 0, available_tenants: 0, contents_period: 0, contents_all: 0, drafts_all: 0, drafts_pending: 0, drafts_saved: 0, drafts_published: 0, drafts_failed: 0, media_active: 0, media_usage: 0 }) : undefined;
  const platformTotals = visibleTenants.reduce<Record<string, { contents: number; contents_period: number; drafts: number }>>((sum, tenant) => {
    for (const [platform, values] of Object.entries(tenant.platforms)) {
      const current = sum[platform] ?? { contents: 0, contents_period: 0, drafts: 0 };
      current.contents += values.contents;
      current.contents_period += values.contents_period;
      current.drafts += values.drafts;
      sum[platform] = current;
    }
    return sum;
  }, {});
  const shownPlatforms = Object.entries(platformTotals).filter(([platform]) => platformFilter === "all" || platform === platformFilter);
  const kpis = [
    ["账号组", total?.tenants ?? "—", `数据库可读取 ${total?.available_tenants ?? "—"} 个`],
    ["内容产出", total?.contents_period ?? "—", `近 ${days} 天`],
    ["内容总量", total?.contents_all ?? "—", "所有可读取账号组"],
    ["待存草稿", total?.drafts_pending ?? "—", "待存入平台的草稿；不等同于人工审核"],
    ["已存平台草稿", total?.drafts_saved ?? "—", "不等同于已发布"],
    ["已发布及后续状态", total?.drafts_published ?? "—", "含已发布、采集和已采集"],
    ["失败草稿", total?.drafts_failed ?? "—", "按失败状态记录统计"],
    ["可用图片素材", total?.media_active ?? "—", "各账号组素材库合计"],
    ["素材使用关系", total?.media_usage ?? "—", "记录配图分配关系"],
  ] as const;

  return (
    <div className="global-monitor-page">
      <div className="page-head">
        <div>
          <div className="eyebrow">CROSS-WORKSPACE OVERVIEW</div>
          <h1 className="page-title">全局监控</h1>
          <div className="page-desc">跨账号组只读汇总。单个数据库不可读取时会单独标记，不会用其他账号组的数据替代。</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          <select aria-label="账号组筛选" value={tenantFilter} onChange={(event) => setTenantFilter(event.target.value)}>
            <option value="all">全部账号组</option>
            {(data?.tenants ?? []).map((tenant) => <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.tenant_name}</option>)}
          </select>
          <select aria-label="内容统计时间范围" value={days} onChange={(event) => setDays(Number(event.target.value))}>
            <option value={1}>最近 24 小时</option><option value={7}>近 7 天</option>
            <option value={30}>近 30 天</option><option value={90}>近 90 天</option><option value={365}>近 365 天</option>
          </select>
          <button className="btn sm" onClick={() => void load()} disabled={loading}>{loading ? "刷新中…" : "刷新"}</button>
        </div>
      </div>

      {error && <div className="panel" role="alert" style={{ color: "var(--red)" }}>{error}</div>}
      {loading && !data ? <div className="panel"><div className="empty-state">正在汇总账号组数据…</div></div> : (
        <>
          <div className="global-monitor-kpis">
            {kpis.map(([label, value, note]) => (
              <section className="panel global-monitor-kpi" key={label}>
                <div className="panel-sub">{label}</div>
                <div className="global-monitor-value">{value}</div>
                <div className="panel-sub">{note}</div>
              </section>
            ))}
          </div>

          <section className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title">平台内容分布</div>
                <div className="panel-sub">当前账号组筛选范围内，按平台汇总内容与草稿记录。</div>
              </div>
              <select aria-label="平台明细筛选" value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)}>
                <option value="all">全部平台</option><option value="xhs">小红书</option><option value="douyin">抖音</option>
                <option value="kuaishou">快手</option><option value="wechat_channels">视频号</option>
              </select>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>平台</th><th>内容总量</th><th>草稿记录</th></tr></thead>
                <tbody>
                  {shownPlatforms.map(([platform, values]) => (
                    <tr key={platform}><td>{PLATFORM_LABELS[platform] ?? platform}</td><td>{values.contents}</td><td>{values.drafts}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title">账号组明细</div>
                <div className="panel-sub">内容产出按所选时间范围；草稿、素材为当前累计状态。</div>
              </div>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>账号组</th><th>数据库</th><th>近 {days} 天内容</th><th>内容总量</th><th>草稿总量</th><th>待处理</th><th>已存草稿</th><th>发布及后续</th><th>可用素材</th><th>素材使用</th></tr></thead>
                <tbody>
                  {visibleTenants.map((tenant) => (
                    <tr key={tenant.tenant_id}>
                      <td><strong>{tenant.tenant_name}</strong></td>
                      <td><span className={tenant.db_available ? "tag green" : "tag orange"}>{tenant.db_available ? "可读取" : "不可用"}</span></td>
                      <td>{tenant.db_available ? tenant.contents_period : "—"}</td>
                      <td>{tenant.db_available ? tenant.contents_all : "—"}</td>
                      <td>{tenant.db_available ? tenant.drafts_all : "—"}</td>
                      <td>{tenant.db_available ? tenant.drafts_pending : "—"}</td>
                      <td>{tenant.db_available ? tenant.drafts_saved : "—"}</td>
                      <td>{tenant.db_available ? tenant.drafts_published : "—"}</td>
                      <td>{tenant.db_available ? tenant.media_active : "—"}</td>
                      <td>{tenant.db_available ? tenant.media_usage : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <div className="note" style={{ marginTop: 12 }}>
            统计说明：内容产出量依据 contents.created_at；草稿按 drafts.status 分类；“待存草稿”不代表人工审核队列；“发布及后续”包含已发布、数据采集中和已采集状态。当前数据模型没有可去重的平台账号身份字段，因此本页不把平台数或账号快照数冒充账号数。
          </div>
        </>
      )}
    </div>
  );
}
