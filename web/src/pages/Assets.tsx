import { useEffect, useState } from "react";
import AsyncPanel from "../components/AsyncPanel";
import EmptyHint from "../components/EmptyHint";
import TopicPoolPane from "../components/TopicPoolPane";
import CreatedContentPane from "../components/CreatedContentPane";
import { useApp } from "../AppContext";
import { PersonaEditor } from "../components/ConfigEditors";
import { useUrlTab } from "../nav";
import {
  api, AssetItem, AssetType, Draft, HotTopicItem, KbInspect, ScoredTopic, SystemConfigView,
} from "../api";

type Tab = "topics" | "scoring" | "created" | "sources" | "voice" | "templates" | "mapping";

const TABS: { key: Tab; label: string }[] = [
  { key: "topics", label: "选题池" },
  { key: "scoring", label: "选题评分" },
  { key: "created", label: "已生产内容" },
  { key: "sources", label: "外部参考" },
  { key: "voice", label: "个人观点与画像" },
  { key: "templates", label: "内容模板" },
  { key: "mapping", label: "字段映射检查" },
];

export default function Assets() {
  const { showToast, withPending, isPending } = useApp();
  const [tab, setTab] = useUrlTab<Tab>(TABS.map((t) => t.key), "topics");
  const [scored, setScored] = useState<{ mode: string; topics: ScoredTopic[] } | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [kb, setKb] = useState<KbInspect | null>(null);
  const [config, setConfig] = useState<SystemConfigView | null>(null);
  const [hot, setHot] = useState<HotTopicItem[]>([]);
  const [assets, setAssets] = useState<Record<AssetType, AssetItem[]>>({ reference: [], voice: [], template: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      // Promise.allSettled — 各请求独立失败不阻断其他成功结果
      // 尤其 hotTopics 是外部公益 API（易挂），不能让它拖垮整个页面
      const results = await Promise.allSettled([
        api.scoreTopics(30),
        api.drafts(),
        api.kbInspect(),
        api.config(),
        api.hotTopics(),
        api.assets("reference"),
        api.assets("voice"),
        api.assets("template"),
      ]);
      // 成功的逐个 set；失败的汇总成 error 提示
      const failures: string[] = [];
      const ok = (i: number) => results[i].status === "fulfilled";
      const v = (i: number) => ok(i) ? (results[i] as PromiseFulfilledResult<any>).value : null;
      if (!ok(0)) failures.push("选题评分"); else setScored(v(0));
      if (!ok(1)) failures.push("草稿列表"); else setDrafts(v(1));
      if (!ok(2)) failures.push("题库检查"); else setKb(v(2));
      if (!ok(3)) failures.push("配置"); else setConfig(v(3));
      if (!ok(4)) failures.push("热点（外部公益 API）"); else setHot(v(4)?.items ?? []);
      if (!ok(5)) failures.push("外部参考资产");
      if (!ok(6)) failures.push("个人画像资产");
      if (!ok(7)) failures.push("模板资产");

      const refs = v(5); const voices = v(6); const templates = v(7);
      setAssets({
        reference: refs?.items ?? [],
        voice: voices?.items ?? [],
        template: templates?.items ?? [],
      });

      if (failures.length > 0) {
        setError(`部分数据加载失败：${failures.join("、")} — 已展示成功部分`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const addAsset = async (type: AssetType, data: { title: string; body?: string; source_url?: string }) => {
    try {
      await withPending(`asset:${type}`, () => api.createAsset({ asset_type: type, ...data }));
      showToast("✓ 资产已保存", "success");
      await load();
      return true;
    } catch (e: any) {
      showToast(`保存失败: ${e?.message ?? e}`, "error");
      return false;
    }
  };

  const total = kb?.stats.total;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">CONTENT ASSETS</div>
          <h1 className="page-title">内容资产</h1>
          <div className="page-desc">
            选题库、已生产内容、外部参考、个人观点与画像、内容模板、字段映射统一归档。
            
          </div>
        </div>
        <div className="head-actions">
          <button className="btn sm" onClick={load} disabled={loading}>↻ 刷新</button>
        </div>
      </div>

      {error && (
        <div className="panel" style={{ borderColor: "var(--red)", marginBottom: 14 }}>
          <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>! 加载失败：{error}</span>
            <button className="btn sm" onClick={load}>↻ 重试</button>
          </div>
        </div>
      )}

      <div className="tabs page-tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "topics" && <TopicPoolPane />}

      {tab === "scoring" && (
        <>
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">★</span>选题评分排行</div>
                <div className="panel-sub">当前评分模式：{({ cold_start: "冷启动", sample_accumulating: "样本积累", stable: "稳定运行" } as Record<string, string>)[scored?.mode ?? ""] ?? "—"} · 按定位、时效、新鲜度加权</div>
              </div>
            </div>
            <AsyncPanel
              loading={loading}
              error={error}
              onRetry={load}
              empty={(scored?.topics.length ?? 0) === 0}
              emptyText={
                <EmptyHint
                  className=""
                  next="到「内容资产 → 字段映射检查」确认选题库可读，再到「内容生产」新建一轮"
                >
                  评分引擎暂无输出
                </EmptyHint>
              }
              loadingText="加载评分结果..."
              boxed={false}
            >
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>#</th><th>选题</th><th>维度</th><th>总分</th>
                      <th>定位</th><th>时效</th><th>新鲜度</th><th>类型</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(scored?.topics ?? []).map((t, i) => (
                      <tr key={t.id}>
                        <td style={{ color: "var(--subtle)", fontSize: 11, fontWeight: 600 }}>#{i + 1}</td>
                        <td style={{ fontWeight: 500 }}>{t.title}</td>
                        <td><span className="tag gray">{t.dimension}</span></td>
                        <td style={{ fontWeight: 700, color: "var(--blue)", fontSize: 14 }}>{t.total_score}</td>
                        <td style={{ fontSize: 11 }}>{_num(t.breakdown.position_match?.raw)}</td>
                        <td style={{ fontSize: 11 }}>{_num(t.breakdown.timeliness?.raw)}</td>
                        <td style={{ fontSize: 11 }}>{_num(t.breakdown.freshness?.raw)}</td>
                        <td>{t.is_exploration ? <span className="tag purple">探索</span> : <span className="tag gray">常规</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </AsyncPanel>
          </div>
        </>
      )}

      {tab === "created" && <CreatedContentPane />}

      {tab === "sources" && (
        <>
          <AssetForm
            type="reference"
            titleLabel="来源标题"
            needUrl
            pending={isPending("asset:reference")}
            onSubmit={addAsset}
          />
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">↗</span>外部参考 · 已录入 {assets.reference.length} 条</div>
                <div className="panel-sub">来源必须可溯源（原始链接 + 发现时间 + 证据状态），不存无法溯源的摘要</div>
              </div>
            </div>
            <AsyncPanel
              loading={loading}
              error={error}
              onRetry={load}
              empty={assets.reference.length === 0}
              emptyText={
                <EmptyHint className="" next="在上方粘贴新闻/文章链接并填写标题，或到「热点雷达」查看已采集热点">
                  暂无外部参考
                </EmptyHint>
              }
              loadingText="加载外部参考..."
              boxed={false}
            >
              <div className="table-wrap">
                <table>
                  <thead><tr><th>标题</th><th>原始链接</th><th>录入时间</th></tr></thead>
                  <tbody>
                    {assets.reference.map((a) => (
                      <tr key={a.id}>
                        <td>{a.title}</td>
                        <td style={{ fontSize: 11 }}>
                          {a.source_url ?? <span style={{ color: "var(--subtle)" }}>—</span>}
                        </td>
                        <td>{a.created_at ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </AsyncPanel>
          </div>

          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">✳</span>热点采集来源（实时）</div>
                <div className="panel-sub">采集失败的来源不显示热度数字</div>
              </div>
            </div>
            {hot.length === 0 ? (
              <EmptyHint next="到「热点雷达」点「立即刷新」触发采集">暂无热点数据</EmptyHint>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>标题</th><th>来源</th><th>发现时间</th><th>热度</th><th>证据状态</th></tr></thead>
                  <tbody>
                    {hot.slice(0, 10).map((h) => (
                      <tr key={h.id}>
                        <td>{h.title}</td>
                        <td><span className="tag gray">{h.source}</span></td>
                        <td>{h.discovered_at ?? <span style={{ color: "var(--subtle)" }}>—</span>}</td>
                        <td>{h.heat == null ? <span style={{ color: "var(--subtle)" }}>—</span> : h.heat.toLocaleString()}</td>
                        <td>
                          <span className={`tag ${h.evidence_status === "collected" ? "green" : h.evidence_status === "demo" ? "orange" : "red"}`}>
                            {h.evidence_status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}

      {tab === "voice" && (
        <>
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">♙</span>账号画像</div>
                <div className="panel-sub">文案生成与终极目标检查共用这份画像，改完立即生效</div>
              </div>
            </div>
            {!config ? (
              <div className="note">加载配置中…</div>
            ) : (
              <PersonaEditor view={config} onSaved={load} />
            )}
            <div className="note" style={{ marginTop: 12 }}>
              红线关键词 {config?.readonly.redline_keywords_count ?? "—"} 个（出现即丢弃重生成，不可关闭）
            </div>
          </div>

          <AssetForm
            type="voice"
            titleLabel="观点标题"
            bodyPlaceholder="例如：AI 教育的价值，不是把答案给得更快，而是帮助孩子更好地思考。"
            pending={isPending("asset:voice")}
            onSubmit={addAsset}
          />
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">✎</span>个人观点库 · {assets.voice.length} 条</div>
                <div className="panel-sub">真实录入，不预置演示观点</div>
              </div>
            </div>
            {assets.voice.length === 0 ? (
              <EmptyHint next="在上方录入一条你真实的立场或判断，后续文案会参考它">暂无个人观点</EmptyHint>
            ) : (
              <div className="list">
                {assets.voice.map((v) => (
                  <div className="task-row" key={v.id}>
                    <i className="task-prio purple" />
                    <div className="task-main">
                      <div className="task-title">{v.title}</div>
                      <div className="task-desc">{v.body || <span style={{ color: "var(--subtle)" }}>（无正文）</span>}</div>
                    </div>
                    <span className="tag gray">{v.created_at?.slice(0, 10) ?? "—"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      {tab === "templates" && (
        <>
          <AssetForm
            type="template"
            titleLabel="模板名"
            bodyPlaceholder="例如：问题引入 → 实测过程 → 结果证据 → 局限 → 结论"
            pending={isPending("asset:template")}
            onSubmit={addAsset}
          />
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">▧</span>可复用内容模板 · {assets.template.length} 条</div>
                <div className="panel-sub">只存结构说明；模板本身不含任何历史数据</div>
              </div>
            </div>
            {assets.template.length === 0 ? (
              <EmptyHint next="在上方录入一套你验证过的图文结构，作为后续内容计划的制作参考">暂无模板</EmptyHint>
            ) : (
              <div className="grid three">
                {assets.template.map((t) => (
                  <div className="skill-card" key={t.id}>
                    <div className="skill-name">{t.title}</div>
                    <div className="skill-desc">{t.body || "（无结构说明）"}</div>
                    <div className="skill-foot"><span>{t.created_at?.slice(0, 10) ?? "—"}</span></div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      {tab === "mapping" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⚙</span>本地选题库字段映射</div>
              <div className="panel-sub">
                检测失败会阻断依赖该数据源的选题扫描，不会用空表或旧快照伪装成同步成功
              </div>
            </div>
            <div className="head-actions">
              <button className="btn sm" onClick={load} disabled={loading}>运行检测</button>
            </div>
          </div>
          {kb && (
            <>
              <div className="note" style={{ marginBottom: 12 }}>
                文件：{kb.path} · 类型 {kb.file_type}
                {kb.table ? ` · 表 ${kb.table}` : ""} ·
                {" "}状态：
                {kb.ok
                  ? <span className="tag green" style={{ marginLeft: 6 }}>必要字段齐全</span>
                  : <span className="tag red" style={{ marginLeft: 6 }}>必要字段缺失，数据源不可用</span>}
              </div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>通用字段</th><th>原始字段</th><th>是否存在于库中</th></tr></thead>
                  <tbody>
                    {Object.entries(kb.field_mapping).map(([k, v]) => {
                      const missing = kb.missing_fields.includes(k);
                      return (
                        <tr key={k}>
                          <td><span className="tag gray">{k}</span></td>
                          <td>{v}</td>
                          <td>{missing ? <span className="tag red">缺失</span> : <span className="tag green">✓</span>}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="note" style={{ marginTop: 12 }}>
                库内总行数：
                {kb.stats.total == null ? " —" : ` ${kb.stats.total.toLocaleString()}`}
                · 唯一维度：
                {kb.stats.unique_dimensions == null ? " —" : ` ${kb.stats.unique_dimensions}`}
                {kb.columns.length > 0 && ` · 实际列：${kb.columns.join(", ")}`}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}

/** 三类资产共用的小表单（只提交真实输入，不做假数据填充） */
function AssetForm({
  type, titleLabel, bodyPlaceholder, needUrl, pending, onSubmit,
}: {
  type: AssetType;
  titleLabel: string;
  bodyPlaceholder?: string;
  needUrl?: boolean;
  pending: boolean;
  onSubmit: (type: AssetType, data: { title: string; body?: string; source_url?: string }) => Promise<boolean>;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [url, setUrl] = useState("");

  const submit = async () => {
    if (!title.trim()) return;
    const ok = await onSubmit(type, {
      title: title.trim(),
      body: body.trim() || undefined,
      source_url: needUrl ? url.trim() : undefined,
    });
    if (ok) { setTitle(""); setBody(""); setUrl(""); }
  };

  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="panel-head">
        <div>
          <div className="panel-title"><span className="icon">＋</span>新增{titleLabel}</div>
          <div className="panel-sub">只写入本地 content_assets 表</div>
        </div>
      </div>
      <div className="grid" style={{ gridTemplateColumns: needUrl ? "1fr 1fr 1.4fr auto" : "1fr 2fr auto", gap: 10 }}>
        <input className="field" placeholder={titleLabel} value={title} onChange={(e) => setTitle(e.target.value)} />
        <input className="field" placeholder={bodyPlaceholder ?? "说明（可空）"} value={body} onChange={(e) => setBody(e.target.value)} />
        {needUrl && <input className="field" placeholder="原始链接 https://…（必填）" value={url} onChange={(e) => setUrl(e.target.value)} />}
        <button className="btn primary sm" disabled={pending || !title.trim() || (needUrl && !url.trim())} onClick={submit}>
          {pending ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}

function _num(v: number | null | undefined) {
  return v == null ? <span style={{ color: "var(--subtle)" }}>—</span> : v.toFixed(0);
}
