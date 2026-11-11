import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { queryClient, queryKeys } from "../query/client";
import { resources } from "../query/resources";
import type { Work } from "../model";
import { catalogRequest, type ManagedPerson } from "./CatalogManagement";

export function useCatalogActions(onChanged: () => void) {
  const { data: prefs } = useQuery(resources.prefs(), queryClient);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  async function run(key: string, action: () => Promise<string>, refresh = true) {
    setBusy(key); setMessage("");
    try {
      setMessage(await action());
      if (refresh) {
        try { sessionStorage.removeItem("mdc:catalog-return"); } catch { /* Storage is optional. */ }
        void queryClient.invalidateQueries({ queryKey: queryKeys.works, refetchType: "none" });
        onChanged();
      }
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(null); }
  }
  return {
    prefs, busy, message, run,
    toggleWant: (work: Work) => run(`want-${work.id}`, async () => {
      const next = await api.setWantList(work.id, !work.want_list);
      queryClient.setQueryData(queryKeys.prefs, next);
      return work.want_list ? "已移出想看" : "已加入想看";
    }),
    refreshWork: (work: Work) => run(`work-${work.id}`, async () => {
      const result = await api.refreshWork(work.id);
      return result.accepted_work_id ? "在线元数据已更新" : "未找到可自动接受的在线结果";
    }),
  };
}
export type CatalogActions = ReturnType<typeof useCatalogActions>;

export function CatalogLibraryTools({ actions }: { actions: CatalogActions }) {
  const [query, setQuery] = useState("");
  return <details><summary>入库与批量工具</summary>
    <form className="command-bar-row" onSubmit={event => {
      event.preventDefault();
      if (!query.trim()) return;
      void actions.run("lookup", async () => {
        const value = query.trim();
        const result = await api.lookupWork(/^https?:\/\//i.test(value) ? { source_url: value } : { code: value });
        return result.work ? `已建立或更新 ${result.work.primary_code ?? result.work.title}` : "未找到可自动确认的结果";
      });
    }}>
      <input aria-label="番号或来源地址" value={query} onChange={event => setQuery(event.target.value)} placeholder="番号、FC2 或详情 URL" />
      <button disabled={!!actions.busy || !query.trim()}>一键查档</button>
      <button type="button" disabled={!!actions.busy} onClick={() => void actions.run("translate", async () => {
        const result = await api.translateWorks();
        return `补翻译：成功 ${result.translated}，无需翻译 ${result.skipped}，失败 ${result.failed}，剩余 ${result.remaining}`;
      })}>补翻译</button>
      <button type="button" disabled={!!actions.busy} onClick={() => void actions.run("collections", async () => {
        const result = await api.seedCollections();
        return `机构与系列：共 ${result.collections_total}，补齐 ${result.collections_created}`;
      })}>补齐机构与系列</button>
    </form>
  </details>;
}

export function CatalogPersonActions({ person, actions }: { person: ManagedPerson; actions: CatalogActions }) {
  const refs = person.refs?.length ? person.refs : [person.ref];
  const keys = new Set([person.id, ...refs.map(ref => ref.replace(/^(jav|nj|person):/, ""))]);
  const subscriptions = actions.prefs?.subscriptions.filter(item => keys.has(item.actor_key)) ?? [];
  const subscribed = subscriptions.some(item => item.enabled);
  return <div className="catalog-person-actions">
    <button disabled={!!actions.busy} onClick={() => void actions.run("subscribe", async () => {
      let next = actions.prefs;
      if (subscribed) {
        for (const item of subscriptions) next = await api.removeSubscription(item.actor_key);
      } else {
        const ref = refs.find(value => value.startsWith("jav:") || value.startsWith("nj:"));
        const actorKey = ref ? ref.slice(ref.indexOf(":") + 1) : person.id;
        next = await api.upsertSubscription({ actor_key: actorKey, actor_name: person.name,
          start_date: new Date().toISOString().slice(0, 10), max_cast: 3, enabled: true });
      }
      if (next) queryClient.setQueryData(queryKeys.prefs, next);
      return subscribed ? "已取消订阅" : "已订阅演员";
    }, false)}>{subscribed ? "取消订阅" : "订阅演员"}</button>
    <button disabled={!!actions.busy} onClick={() => void actions.run("blacklist", async () => {
      await catalogRequest("people/" + person.id + "/preference", "PUT", {
        favorite: person.favorite, blacklist: !person.blacklist,
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.prefs });
      return person.blacklist ? "已移出黑名单" : "已加入黑名单";
    })}>{person.blacklist ? "移出黑名单" : "加入黑名单"}</button>
  </div>;
}
