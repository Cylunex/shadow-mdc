import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { appUrl } from "../api";
import { copyToClipboard } from "../lib/clipboard";
import { tvboxRequest, type TvBoxGroup, type TvBoxSite } from "./tvboxApi";

type Binding = { needs_rotation?: boolean; id: string; name: string; group_id: string | null; revoked: boolean; expires_at: string | null };
type Issued = Binding & { path: string };
type Pack = { id: string; label: string; site_count: number; checked_at: string; sync_minutes: number; remote: boolean };
type RelayRevision = { id: string; source: string; state: string };

export function TvBoxManage({ sites, groups, onImport, allowHttp }: {
  sites: TvBoxSite[]; groups: TvBoxGroup[]; onImport: (id: string) => void; allowHttp: boolean;
}) {
  const cache = useQueryClient();
  const [showRelay, setShowRelay] = useState(false);
  const [legacy, setLegacy] = useState("");
  const [siteHistory, setSiteHistory] = useState("");
  const [siteName, setSiteName] = useState("");
  const [groupId, setGroupId] = useState("");
  const [groupName, setGroupName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [bindingName, setBindingName] = useState("");
  const [bindingGroup, setBindingGroup] = useState("");
  const [bindingDays, setBindingDays] = useState("");
  const [issued, setIssued] = useState<Issued>();
  const [copied, setCopied] = useState<"" | "multi" | "single">("");
  const revisions = useQuery({
    queryKey: ["tvbox", "revisions", siteHistory], enabled: Boolean(siteHistory),
    queryFn: ({ signal }) => tvboxRequest<{ id: string; created_at: string }[]>(
      `/sites/${siteHistory}/revisions`, "GET", undefined, signal,
    ),
  });
  const packs = useQuery({
    queryKey: ["tvbox", "packs"],
    queryFn: ({ signal }) => tvboxRequest<Pack[]>("/packs", "GET", undefined, signal),
  });
  const relayHistory = useQuery({
    queryKey: ["tvbox", "relay-history"], enabled: showRelay,
    queryFn: ({ signal }) => tvboxRequest<RelayRevision[]>("/relay-history", "GET", undefined, signal),
  });
  const job = useMutation({
    mutationFn: (path: string) => tvboxRequest<{ id: string }>(path, "POST"),
    onSuccess: (response) => onImport(response.id),
  });
  const subscriptions = useQuery({
    queryKey: ["tvbox", "subscriptions"],
    queryFn: ({ signal }) => tvboxRequest<Binding[]>("/subscriptions", "GET", undefined, signal),
  });
  const mutate = useMutation({
    mutationFn: ({ path, method, data }: { path: string; method: string; data?: unknown }) =>
      tvboxRequest<Partial<Issued>>(path, method, data),
    onSuccess: async (reply) => {
      if (reply.path) { setIssued(reply as Issued); setCopied(""); }
      await cache.invalidateQueries({ queryKey: ["tvbox"] });
    },
  });

  function edit(id: string) {
    const group = groups.find((row) => row.id === id);
    setGroupId(id);
    setGroupName(group?.name ?? "");
    setMembers(group?.site_ids ?? []);
  }

  const link = issued && new URL(appUrl(issued.path)!, window.location.href).href;
  return <div className="tvbox-admin">
    <section className="tvbox-surface">
      <h2>站点</h2>
      {sites.length === 0 && <p className="muted">导入源后可在这里管理。</p>}
      <ul className="tvbox-manage-list">{sites.map((site) => <li key={site.id}>
        <div><strong>{site.name}</strong>
          <p className="muted">{site.enabled ? site.active_revision ? "可使用" : "待通过测试" : "已停用"}
            {site.hidden ? " · 已隐藏" : ""}{site.pinned ? " · 已固定版本" : ""}
            {site.failure ? ` · ${site.failure}` : ""}</p>
        </div>
        <div className="tvbox-actions">
          <button type="button" className="ghost" disabled={mutate.isPending} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}/refresh`, method: "POST" })}>重测</button>
          <button type="button" className="ghost" disabled={mutate.isPending} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { enabled: !site.enabled } })}>
            {site.enabled ? "停用" : "启用"}</button>
          <button type="button" className="ghost" disabled={mutate.isPending} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { hidden: !site.hidden } })}>
            {site.hidden ? "取消隐藏" : "隐藏"}</button>
          <button type="button" className="ghost" disabled={mutate.isPending || !site.active_revision} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { pinned: !site.pinned } })}>
            {site.pinned ? "恢复自动更新" : "固定修订"}</button>
          <button type="button" className="ghost" onClick={() => {
            setSiteHistory(siteHistory === site.id ? "" : site.id); setSiteName(site.name);
          }}>名称与修订</button>
          <button type="button" className="ghost" disabled={mutate.isPending || !site.previous_revision} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { rollback: true } })}>回滚</button>
          <button type="button" className="ghost" disabled={mutate.isPending || site.priority >= 1000} onClick={() =>
            mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { priority: site.priority + 1 } })}>提升优先级</button>
        </div>
        {siteHistory === site.id && <form className="tvbox-form" onSubmit={(event) => {
          event.preventDefault();
          if (siteName.trim()) mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { name: siteName.trim() } });
        }}>
          <label>站点名称<input value={siteName} maxLength={200} onChange={(event) => setSiteName(event.target.value)} /></label>
          <button disabled={mutate.isPending || !siteName.trim()}>保存名称</button>
          <label>切换到历史修订<select value={site.active_revision ?? ""} disabled={mutate.isPending}
            onChange={(event) => mutate.mutate({ path: `/sites/${site.id}`, method: "PATCH", data: { revision_id: event.target.value } })}>
            <option value="" disabled>选择修订</option>
            {revisions.data?.map((rev) => <option key={rev.id} value={rev.id}>
              {new Date(rev.created_at).toLocaleString()} · {rev.id.slice(0, 8)}
            </option>)}
          </select></label>
        </form>}
      </li>)}</ul>
    </section>
    <section className="tvbox-surface">
      <h2>配置更新与迁移记录</h2>
      <ul className="tvbox-manage-list">{packs.data?.map((pack) => <li key={pack.id}>
        <div>{pack.label} · {pack.site_count} 站
          <p className="muted">{new Date(pack.checked_at).toLocaleString()}
            {pack.remote ? pack.sync_minutes > 0 ? ` · 每 ${pack.sync_minutes} 分钟更新` : " · 手动更新" : ""}</p>
        </div>
        {pack.remote && <button type="button" className="ghost" disabled={job.isPending} onClick={() =>
          job.mutate(`/packs/${pack.id}/refresh`)}>立即更新</button>}
      </li>)}</ul>
      <details open={showRelay} onToggle={(event) => setShowRelay(event.currentTarget.open)}>
        <summary>Relay 历史修订</summary>
        {relayHistory.isSuccess && !relayHistory.data.length && <p className="muted">暂无导入的历史修订</p>}
        {!!relayHistory.data?.length && <div className="tvbox-actions">
          <select aria-label="Relay 历史修订" value={legacy} onChange={(event) => setLegacy(event.target.value)}>
            <option value="">选择历史修订</option>
            {relayHistory.data.map((row) => <option key={row.id} value={row.id}>
              {row.source} · {row.state || "已归档"} · {row.id.slice(0, 12)}
            </option>)}
          </select>
          <button type="button" className="ghost" disabled={!legacy || job.isPending} onClick={() =>
            job.mutate(`/relay-history/${encodeURIComponent(legacy)}/restore?allow_http=${allowHttp}`)}>重新测试此修订</button>
        </div>}
      </details>
    </section>
    <section className="tvbox-surface">
      <h2>仓库分组</h2>
      <form className="tvbox-form" onSubmit={(event) => {
        event.preventDefault();
        mutate.mutate({
          path: groupId ? `/groups/${groupId}` : "/groups", method: groupId ? "PUT" : "POST",
          data: { name: groupName.trim(), site_ids: members },
        }, { onSuccess: (reply) => { if (reply.id) setGroupId(reply.id); } });
      }}>
        <label>编辑仓库<select value={groupId} onChange={(event) => edit(event.target.value)}>
          <option value="">新建仓库</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
        </select></label>
        <label>仓库名称<input value={groupName} maxLength={120} onChange={(event) => setGroupName(event.target.value)} required /></label>
        <button disabled={mutate.isPending || !groupName.trim()}>保存分组</button>
        <fieldset className="tvbox-members"><legend>站点成员</legend>{sites.map((site) => <label key={site.id}>
          <input type="checkbox" checked={members.includes(site.id)} onChange={(event) =>
            setMembers(event.target.checked ? [...members, site.id] : members.filter((id) => id !== site.id))} />{site.name}
        </label>)}</fieldset>
      </form>
    </section>
    <section className="tvbox-surface">
      <h2>TVBox / 影视仓订阅</h2>
      <form className="tvbox-form" onSubmit={(event) => {
        event.preventDefault();
        mutate.mutate({ path: "/subscriptions", method: "POST", data: {
          name: bindingName.trim(), group_id: bindingGroup || null, days: bindingDays ? Number(bindingDays) : null,
        } });
      }}>
        <label>订阅名称<input value={bindingName} maxLength={120} onChange={(event) => setBindingName(event.target.value)} required /></label>
        <label>订阅范围<select value={bindingGroup} onChange={(event) => setBindingGroup(event.target.value)}>
          <option value="">全部仓库</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
        </select></label>
        <label>有效天数<input type="number" min={1} max={365} placeholder="长期有效" value={bindingDays} onChange={(event) => setBindingDays(event.target.value)} /></label>
        <button disabled={mutate.isPending || !bindingName.trim()}>创建订阅</button>
      </form>
      {link && <div className="tvbox-subscription-link">
        <label>多仓订阅地址<input readOnly value={link} onFocus={(event) => event.target.select()} /></label>
        <button className="ghost" type="button" onClick={() =>
          void copyToClipboard(link).then((ok) => setCopied(ok ? "multi" : ""))
        }>{copied === "multi" ? "已复制" : "复制多仓地址"}</button>
        <label>单仓订阅地址<input readOnly value={link.replace(/store\.json$/, "sites.json")} onFocus={(event) => event.target.select()} /></label>
        <div className="tvbox-actions">
          <button className="ghost" type="button" onClick={() =>
            void copyToClipboard(link.replace(/store\.json$/, "sites.json")).then((ok) => setCopied(ok ? "single" : ""))
          }>{copied === "single" ? "已复制" : "复制单仓地址"}</button>
          <button className="ghost" type="button" onClick={() => setIssued(undefined)}>收起地址</button>
        </div>
      </div>}
      <ul className="tvbox-manage-list">{subscriptions.data?.map((binding) => {
        const expired = binding.expires_at !== null && Date.parse(binding.expires_at) <= Date.now();
        return <li key={binding.id}>
          <div>{binding.name}<p className="muted">{binding.revoked ? "已停用" : expired ? "已过期" : binding.needs_rotation ? "待重签"
            : groups.find((group) => group.id === binding.group_id)?.name ?? "全部仓库"}</p></div>
          <div className="tvbox-actions">
            <button className="ghost" type="button" disabled={mutate.isPending || binding.revoked || expired} onClick={() =>
              mutate.mutate({ path: `/subscriptions/${binding.id}/rotate`, method: "POST" })}>重签地址</button>
            <button className="ghost" type="button" disabled={mutate.isPending || binding.revoked} onClick={() => {
              mutate.mutate({ path: `/subscriptions/${binding.id}`, method: "DELETE" });
              if (issued?.id === binding.id) setIssued(undefined);
            }}>停用订阅</button>
          </div>
        </li>;
      })}</ul>
    </section>
    {(mutate.error || subscriptions.error || revisions.error || packs.error || relayHistory.error || job.error) && <p role="alert">{(mutate.error || subscriptions.error || revisions.error || packs.error || relayHistory.error || job.error)?.message}</p>}
  </div>;
}
