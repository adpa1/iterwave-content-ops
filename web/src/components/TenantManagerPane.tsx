import { useCallback, useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { api, TenantListResponse } from "../api";

export default function TenantManagerPane() {
  const { showToast, withPending, isPending } = useApp();
  const [data, setData] = useState<TenantListResponse | null>(null);
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await api.tenants());
    } catch (e) {
      setError(e instanceof Error ? e.message : "账号组信息加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    const id = tenantId.trim().toLowerCase();
    if (!id || !name.trim()) {
      showToast("请填写账号组标识和名称", "error");
      return;
    }
    await withPending("tenant:create", async () => {
      try {
        const result = await api.createTenant({ tenant_id: id, name: name.trim() });
        showToast(`账号组「${result.tenant.name}」已创建`, "success");
        setTenantId("");
        setName("");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "创建账号组失败", "error");
      }
    });
  };

  const select = async (id: string) => {
    await withPending("tenant:select", async () => {
      try {
        const result = await api.selectTenant(id);
        showToast(result.message, result.restart_required ? "info" : "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "切换账号组失败", "error");
      }
    });
  };

  return (
    <div>
      <section className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">账号组管理</div>
            <div className="panel-sub">一个运营者管理多个业务空间。每个账号组拥有独立数据库、策略、素材、登录态和加工结果；原始热点采集数据可共享。</div>
          </div>
          <button className="btn sm" onClick={() => void load()} disabled={loading}>刷新</button>
        </div>
        {data?.restart_required && (
          <div className="panel" style={{ borderColor: "var(--orange)", marginBottom: 12 }}>
            <strong>账号组切换待生效</strong>
            <div className="panel-sub">
              当前运行实例仍使用「{data.runtime_tenant_id}」，已选择「{data.selected_tenant_id}」。
              为避免后台任务在切换过程中写入错误空间，需要手动重启后端服务，切换才会生效。
            </div>
          </div>
        )}
        {error && <div className="panel-sub" style={{ color: "var(--red)" }}>{error}</div>}
        {loading && !data ? <div className="empty-state">正在加载账号组…</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>账号组名称</th><th>标识</th><th>创建时间</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {(data?.items ?? []).map((item) => (
                  <tr key={item.id}>
                    <td>{item.name}</td>
                    <td><code>{item.id}</code></td>
                    <td>{item.created_at ?? "—"}</td>
                    <td>
                      {item.id === data?.runtime_tenant_id
                        ? <span className="tag green">当前运行</span>
                        : item.id === data?.selected_tenant_id
                          ? <span className="tag orange">待切换</span>
                          : <span className="tag gray">未选中</span>}
                    </td>
                    <td>
                      <button className="btn sm" disabled={item.id === data?.selected_tenant_id || isPending("tenant:select")} onClick={() => void select(item.id)}>
                        选择
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <div className="panel-title">创建账号组</div>
            <div className="panel-sub">标识仅允许小写字母、数字、下划线和连字符；创建后不会复制默认账号组的业务数据。</div>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
          <label className="field">
            <span className="field-label">账号组标识</span>
            <input value={tenantId} maxLength={48} onChange={(event) => setTenantId(event.target.value.toLowerCase())} placeholder="例如：brand_a" />
          </label>
          <label className="field">
            <span className="field-label">显示名称</span>
            <input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} placeholder="例如：品牌 A 业务" />
          </label>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
          <button className="btn primary sm" disabled={!tenantId.trim() || !name.trim() || isPending("tenant:create")} onClick={() => void create()}>
            {isPending("tenant:create") ? "创建中…" : "创建账号组"}
          </button>
        </div>
      </section>
    </div>
  );
}
