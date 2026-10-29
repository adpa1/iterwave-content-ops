import { useCallback, useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { api, TenantBackupItem, TenantListResponse } from "../api";

export default function TenantManagerPane() {
  const { showToast, withPending, isPending } = useApp();
  const [data, setData] = useState<TenantListResponse | null>(null);
  const [backups, setBackups] = useState<TenantBackupItem[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [tenantData, backupData] = await Promise.all([api.tenants(), api.tenantBackups()]);
      setData(tenantData);
      setBackups(backupData.items);
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

  const createBackup = async () => {
    await withPending("tenant:backup", async () => {
      try {
        const result = await api.createTenantBackup();
        showToast(`备份已创建：${result.backup_name}`, "success");
        await load();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "创建备份失败", "error");
      }
    });
  };

  const restoreBackup = async (backup: TenantBackupItem) => {
    const confirmed = window.confirm(
      `确定安排恢复到 ${backup.backup_name} 吗？恢复会在后端重启时替换当前账号组数据库、图片素材和运行配置。重启前的新写入不会保留；登录态和 API 密钥不会从备份恢复。`,
    );
    if (!confirmed) return;
    await withPending("tenant:restore", async () => {
      try {
        const result = await api.stageTenantBackupRestore(backup.backup_name, true);
        showToast(result.message, "info");
      } catch (e) {
        showToast(e instanceof Error ? e.message : "安排恢复失败", "error");
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
            <div className="panel-title">当前账号组备份与恢复</div>
            <div className="panel-sub">快照包含数据库、活动图片素材和运行配置覆盖；不包含平台登录态、浏览器 Cookie、API 密钥或共享原始热点数据。</div>
          </div>
          <button className="btn primary sm" onClick={() => void createBackup()} disabled={isPending("tenant:backup")}>
            {isPending("tenant:backup") ? "备份中…" : "立即创建备份"}
          </button>
        </div>
        {backups.length === 0 ? <div className="empty-state">当前账号组暂无快照备份。</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>备份文件</th><th>创建时间</th><th>大小</th><th>操作</th></tr></thead>
              <tbody>
                {backups.map((backup) => (
                  <tr key={backup.backup_name}>
                    <td><code>{backup.backup_name}</code></td>
                    <td>{backup.created_at}</td>
                    <td>{(backup.size_bytes / 1024 / 1024).toFixed(1)} MB</td>
                    <td>
                      <button className="btn sm" disabled={isPending("tenant:restore")} onClick={() => void restoreBackup(backup)}>安排恢复</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="panel-sub" style={{ marginTop: 8 }}>恢复先进行备份清单、路径、哈希与 SQLite 完整性校验，再在下次后端启动、打开数据库前执行。失败时会保留数据库回滚副本。</div>
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
