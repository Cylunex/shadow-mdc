import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";
import { invalidateLibrary, queryClient, queryKeys } from "./query/client";
import { resources } from "./query/resources";
import { TaskCenter } from "./components/TaskCenter";
import { TopNav, type AppView } from "./components/TopNav";
import type { Asset, Candidate, LibraryPrefs, TaskRun, Work } from "./model";
import { Actors, Inbox, Works } from "./panels";
import { CategoriesView } from "./views/CategoriesView";
import { WorkDetailView } from "./views/WorkDetailView";
import { SwipeFeedView } from "./views/SwipeFeedView";
import {
  ActorDetailView,
  findActorRefByName,
  resolveActorDetail
} from "./views/ActorDetailView";
import {
  patchWorksFilters,
  type WorksFilterState
} from "./lib/worksFilters";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function readWorkIdFromUrl(): string | null {
  try {
    return new URLSearchParams(window.location.search).get("work");
  } catch {
    return null;
  }
}

function readActorRefFromUrl(): string | null {
  try {
    return new URLSearchParams(window.location.search).get("actor");
  } catch {
    return null;
  }
}

const APP_VIEWS: ReadonlySet<string> = new Set([
  "actors",
  "works",
  "categories",
  "rankings",
  "swipe",
  "subscriptions",
  "tasks",
  "settings"
]);

/** Deep link for a top-nav tab (`?view=actors`); detail params win over it. */
function readViewFromUrl(): AppView | null {
  try {
    const value = new URLSearchParams(window.location.search).get("view");
    return value && APP_VIEWS.has(value) ? (value as AppView) : null;
  } catch {
    return null;
  }
}

function writeViewToUrl(view: AppView): void {
  const url = new URL(window.location.href);
  if (APP_VIEWS.has(view) && view !== "works") url.searchParams.set("view", view);
  else url.searchParams.delete("view");
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) window.history.replaceState({ view }, "", next);
}

function writeDetailToUrl(opts: { workId?: string | null; actorRef?: string | null }): void {
  const url = new URL(window.location.href);
  const workId = opts.workId ?? null;
  const actorRef = opts.actorRef ?? null;
  if (workId) {
    url.searchParams.set("work", workId);
    url.searchParams.delete("actor");
  } else if (actorRef) {
    url.searchParams.set("actor", actorRef);
    url.searchParams.delete("work");
  } else {
    url.searchParams.delete("work");
    url.searchParams.delete("actor");
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) window.history.pushState({ workId, actorRef }, "", next);
}

function applyWorksFilterPatch(
  patch: Partial<WorksFilterState>,
  options: { replaceAll?: boolean; message?: string } = {}
): void {
  patchWorksFilters(patch, {
    replaceAll: options.replaceAll,
    clearDetail: true,
    mode: "push"
  });
}


const SettingsView = lazy(() => import("./views/SettingsView").then((module) => ({ default: module.SettingsView })));
const DiscoverPanel = lazy(() => import("./components/DiscoverPanel").then((module) => ({ default: module.DiscoverPanel })));
const SubscriptionsView = lazy(() => import("./views/SubscriptionsView").then((module) => ({ default: module.SubscriptionsView })));
const EMPTY_ROWS: never[] = [];
const EMPTY_PREFS: LibraryPrefs = { want_list: [], actor_tags: {}, subscriptions: [], queue: [] };


export function App() {
  const initialWorkId = typeof window !== "undefined" ? readWorkIdFromUrl() : null;
  const initialActorRef = typeof window !== "undefined" ? readActorRefFromUrl() : null;
  const initialView = typeof window !== "undefined" ? readViewFromUrl() : null;
  const [view, setView] = useState<AppView>(
    initialWorkId ? "work-detail" : initialActorRef ? "actor-detail" : initialView ?? "works"
  );
  const [selectedWorkId, setSelectedWorkId] = useState<string | null>(initialWorkId);
  const [selectedActorRef, setSelectedActorRef] = useState<string | null>(initialActorRef);
  const [worksFilterEpoch, setWorksFilterEpoch] = useState(0);
  const [worksListScroll, setWorksListScroll] = useState(0);
  const [actorsListScroll, setActorsListScroll] = useState(0);
  const [detailReturnView, setDetailReturnView] = useState<"works" | "actors" | "actor-detail">(
    initialActorRef && !initialWorkId ? "actors" : "works"
  );
  const [taskTab, setTaskTab] = useState<"inbox" | "runs">("runs");
  const [swipeNeedsWorks, setSwipeNeedsWorks] = useState(false);
  const showingActors = view === "actors" || view === "actor-detail";
  const libraryQuery = useQuery({ ...resources.libraries(), enabled: view === "settings" || view === "tasks" }, queryClient);
  const worksQuery = useQuery({ ...resources.works(), enabled: view === "works" || (view === "swipe" && swipeNeedsWorks) }, queryClient);
  const actorsQuery = useQuery({ ...resources.actors(), enabled: showingActors }, queryClient);
  const nonJavQuery = useQuery({ ...resources.nonJavActors(), enabled: showingActors }, queryClient);
  const inboxQuery = useQuery({ ...resources.inbox(), enabled: view === "tasks" }, queryClient);
  const tasksQuery = useQuery({ ...resources.tasks(), enabled: view === "tasks" }, queryClient);
  const prefsQuery = useQuery(resources.prefs(), queryClient);
  const libraries = libraryQuery.data ?? EMPTY_ROWS;
  const assets = inboxQuery.data ?? EMPTY_ROWS;
  const workRows = worksQuery.data ?? EMPTY_ROWS;
  const actors = actorsQuery.data ?? EMPTY_ROWS;
  const nonJavActors = nonJavQuery.data ?? EMPTY_ROWS;
  const tasks = tasksQuery.data ?? EMPTY_ROWS;
  const prefs = prefsQuery.data ?? EMPTY_PREFS;
  const setPrefs = useCallback((next: LibraryPrefs) => { queryClient.setQueryData(queryKeys.prefs, next); }, []);
  const setWorks = useCallback((next: Work[] | ((current: Work[]) => Work[])) => {
    queryClient.setQueryData<Work[]>(queryKeys.works, (current) =>
      typeof next === "function" ? (current ? next(current) : undefined) : next);
  }, []);
  const setTasks = useCallback((next: TaskRun[]) => {
    void queryClient.cancelQueries({ queryKey: queryKeys.tasks });
    queryClient.setQueryData(queryKeys.tasks, next);
  }, []);
  const lastTasks = useRef<TaskRun[] | undefined>(undefined);
  useEffect(() => {
    const next = tasksQuery.data;
    const previous = lastTasks.current;
    const finished = new Map(previous?.map((task) => [task.id, task.finished_at]));
    if (previous && next?.some((task) => task.finished_at
      && task.finished_at !== finished.get(task.id))) {
      void invalidateLibrary();
      void queryClient.invalidateQueries({ queryKey: queryKeys.prefs });
    }
    lastTasks.current = next;
  }, [tasksQuery.data]);
  const [candidates, setCandidates] = useState<Record<string, Candidate[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("正在连接 Shadow MDC…");
  const loaded = {
    works: worksQuery.data !== undefined,
    actors: actorsQuery.data !== undefined || nonJavQuery.data !== undefined,
    tasks: inboxQuery.data !== undefined,
  };
  const wantIds = useMemo(() => new Set(prefs.want_list), [prefs.want_list]);
  const works = useMemo(
    () => workRows.map((item) => item.want_list === wantIds.has(item.id)
      ? item : { ...item, want_list: wantIds.has(item.id) }),
    [workRows, wantIds],
  );
  const refreshCore = useCallback(async () => {
    await queryClient.fetchQuery({ ...resources.prefs(), staleTime: 0 });
  }, []);
  const refreshWorks = useCallback(async () => {
    if (view === "works" || (view === "swipe" && swipeNeedsWorks)) {
      await queryClient.fetchQuery({ ...resources.works(), staleTime: 0 });
    } else await queryClient.invalidateQueries({ queryKey: queryKeys.works, refetchType: "none" });
  }, [view, swipeNeedsWorks]);
  const refreshActors = useCallback(async () => {
    if (!showingActors) {
      await Promise.all([queryKeys.actors, queryKeys.nonJavActors].map((queryKey) =>
        queryClient.invalidateQueries({ queryKey, refetchType: "none" })));
      return;
    }
    await Promise.all([
      queryClient.fetchQuery({ ...resources.actors(), staleTime: 0 }),
      queryClient.fetchQuery({ ...resources.nonJavActors(), staleTime: 0 }),
    ]);
  }, [showingActors]);
  const refreshInbox = useCallback(async () => {
    if (view === "tasks") await queryClient.fetchQuery({ ...resources.inbox(), staleTime: 0 });
    else await queryClient.invalidateQueries({ queryKey: queryKeys.inbox, refetchType: "none" });
  }, [view]);
  const refreshTasks = useCallback(async () => { await queryClient.fetchQuery(resources.tasks()); }, []);

  useEffect(() => {
    const error = [prefsQuery.error, libraryQuery.error, worksQuery.error, actorsQuery.error,
      nonJavQuery.error, inboxQuery.error, tasksQuery.error].find(Boolean);
    if (error) setMessage(errorMessage(error));
    else if (prefsQuery.data) setMessage("数据已同步");
  }, [prefsQuery.error, prefsQuery.data, libraryQuery.error, worksQuery.error, actorsQuery.error,
    nonJavQuery.error, inboxQuery.error, tasksQuery.error]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        // allow re-focus even from other inputs
      }
      event.preventDefault();
      const lookup = document.getElementById("shadow-mdc-lookup") as HTMLInputElement | null;
      const search = document.getElementById("shadow-mdc-search") as HTMLInputElement | null;
      const el = lookup ?? search;
      if (el) {
        el.focus();
        el.select();
      } else {
        setView("works");
        window.setTimeout(() => {
          const next = (document.getElementById("shadow-mdc-lookup")
            ?? document.getElementById("shadow-mdc-search")) as HTMLInputElement | null;
          next?.focus();
          next?.select();
        }, 50);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const inbox = useMemo(() => assets.filter((asset) => asset.state !== "identified"), [assets]);

  async function run(key: string, action: () => Promise<void>, refresh: "none" | "works" | "actors" | "inbox" | "tasks" | "core" | "all" = "all") {
    setBusy(key);
    try {
      await action();
      await Promise.all([
        ...(refresh === "works" || refresh === "all" ? [refreshWorks()] : []),
        ...(refresh === "actors" || refresh === "all" ? [refreshActors()] : []),
        ...(refresh === "inbox" || refresh === "all" ? [refreshInbox()] : []),
        ...(refresh === "tasks" ? [refreshTasks()] : []),
        ...(refresh === "core" || refresh === "all" ? [refreshCore()] : []),
      ]);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function identify(asset: Asset, payload: { title?: string; source_url?: string }) {
    await run(asset.id, async () => {
      const result = await api.identify(asset.id, payload);
      const next = await api.candidates(asset.id);
      setCandidates((current) => ({ ...current, [asset.id]: next }));
      const failures = result.failures.map((failure) => `${failure.provider}: ${failure.reason}`).join("；");
      setMessage(
        result.accepted_work_id
          ? "已按高置信结果自动识别"
          : `得到 ${result.candidate_ids.length} 个候选${failures ? `；${failures}` : ""}`
      );
    }, "inbox");
  }

  async function loadCandidates(assetId: string) {
    await run(`candidate-${assetId}`, async () => {
      const next = await api.candidates(assetId);
      setCandidates((current) => ({ ...current, [assetId]: next }));
    }, "none");
  }

  async function createManualCandidate(asset: Asset, title: string | undefined) {
    await run(`manual-${asset.id}`, async () => {
      const candidate = await api.manualCandidate(asset.id, title ? { title } : {});
      const next = await api.candidates(asset.id);
      setCandidates((current) => ({ ...current, [asset.id]: next }));
      setMessage(`已生成本地候选：${candidate.record.title}`);
    }, "none");
  }

  async function accept(candidate: Candidate) {
    await run(candidate.id, async () => {
      await api.accept(candidate.id);
      setMessage(`已接受 ${candidate.record.provider} 候选`);
    }, "all");
  }


  const openWorkDetail = useCallback((workId: string) => {
    if (view === "works") setWorksListScroll(window.scrollY);
    if (view === "actor-detail") setDetailReturnView("actor-detail");
    else if (view === "actors" || view === "works") setDetailReturnView(view);
    setSelectedWorkId(workId);
    setView("work-detail");
    writeDetailToUrl({ workId });
  }, [view]);

  const openActorDetail = useCallback((actorRef: string) => {
    if (view === "actors") setActorsListScroll(window.scrollY);
    if (view === "work-detail") setDetailReturnView("works");
    else if (view === "actors" || view === "works") setDetailReturnView(view === "actors" ? "actors" : "works");
    setSelectedActorRef(actorRef);
    setSelectedWorkId(null);
    setView("actor-detail");
    writeDetailToUrl({ actorRef });
  }, [view]);

  const navigateToFilteredWorks = useCallback((
    patch: Partial<WorksFilterState>,
    message: string,
    replaceAll = true
  ) => {
    if (view === "works") setWorksListScroll(window.scrollY);
    setSelectedWorkId(null);
    setSelectedActorRef(null);
    applyWorksFilterPatch(patch, { replaceAll });
    setWorksFilterEpoch((value) => value + 1);
    setView("works");
    setMessage(message);
    requestAnimationFrame(() => window.scrollTo(0, 0));
  }, [view]);

  const closeWorkDetail = useCallback(() => {
    setSelectedWorkId(null);
    if (detailReturnView === "actor-detail" && selectedActorRef) {
      setView("actor-detail");
      writeDetailToUrl({ actorRef: selectedActorRef });
      return;
    }
    const next = detailReturnView === "actor-detail" ? "actors" : detailReturnView;
    setView(next);
    writeDetailToUrl({});
    if (next === "works") {
      setWorksFilterEpoch((value) => value + 1);
      requestAnimationFrame(() => window.scrollTo(0, worksListScroll));
    } else if (next === "actors") {
      requestAnimationFrame(() => window.scrollTo(0, actorsListScroll));
    }
  }, [worksListScroll, actorsListScroll, detailReturnView, selectedActorRef]);

  const closeActorDetail = useCallback(() => {
    setSelectedActorRef(null);
    if (detailReturnView === "works") {
      setView("works");
      writeDetailToUrl({});
      setWorksFilterEpoch((value) => value + 1);
      requestAnimationFrame(() => window.scrollTo(0, worksListScroll));
      return;
    }
    setView("actors");
    writeDetailToUrl({});
    requestAnimationFrame(() => window.scrollTo(0, actorsListScroll));
  }, [actorsListScroll, worksListScroll, detailReturnView]);

  useEffect(() => {
    function onPopState() {
      const workId = readWorkIdFromUrl();
      const actorRef = readActorRefFromUrl();
      if (workId) {
        setSelectedWorkId(workId);
        setView("work-detail");
      } else if (actorRef) {
        setSelectedActorRef(actorRef);
        setSelectedWorkId(null);
        setView("actor-detail");
      } else if (view === "work-detail" || view === "actor-detail") {
        setSelectedWorkId(null);
        setSelectedActorRef(null);
        setView(detailReturnView === "actor-detail" ? "actors" : detailReturnView);
      }
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [view, detailReturnView]);

  const badges = {
    tasks: inbox.length || undefined,
    subscriptions: prefs.queue.filter((item) => item.status === "pending").length || undefined
  };

  return (
    <div className={view === "swipe" ? "app-shell swipe-mode" : "app-shell"}>
      <TopNav
        view={view}
        onChange={(next) => {
          if (next === "works") {
            setSelectedWorkId(null);
            setSelectedActorRef(null);
            writeDetailToUrl({});
            // Keep URL filter params so list↔detail round trips survive.
          } else if (next === "actors") {
            setSelectedWorkId(null);
            setSelectedActorRef(null);
            writeDetailToUrl({});
          } else if (next !== "work-detail" && next !== "actor-detail") {
            setSelectedWorkId(null);
            setSelectedActorRef(null);
            writeDetailToUrl({});
          }
          setView(next);
          if (next !== "work-detail" && next !== "actor-detail") writeViewToUrl(next);
          if (next === "tasks" && inbox.length > 0) setTaskTab("inbox");
        }}
        badges={badges}
      />
      <main className="app-main">
        <div className="status-bar">
          <span>{message}</span>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              void (async () => {
                try {
                  await queryClient.cancelQueries({ queryKey: queryKeys.all });
                  await queryClient.invalidateQueries({ queryKey: queryKeys.all }, { throwOnError: true });
                  setMessage("已强制刷新");
                } catch (error) {
                  setMessage(errorMessage(error));
                }
              })();
            }}
          >
            刷新
          </button>
        </div>

        {view === "actors" && !loaded.actors && (actorsQuery.isPending || nonJavQuery.isPending) && (
          <div className="skeleton-grid" aria-busy="true" aria-label="演员加载中">
            {Array.from({ length: 6 }, (_, index) => (
              <div className="skeleton-card" key={index}>
                <div className="skeleton-poster skeleton-poster--portrait" />
                <div className="skeleton-lines">
                  <span /><span /><span />
                </div>
              </div>
            ))}
          </div>
        )}
        {view === "actors" && (actorsQuery.isFetching || nonJavQuery.isFetching) && loaded.actors && (
          <p className="muted" role="status">演员资料仍在更新…</p>
        )}
        {view === "works" && worksQuery.error && <p role="alert">影片加载失败：{worksQuery.error.message}，可点击上方刷新重试。</p>}
        {view === "actors" && (actorsQuery.error || nonJavQuery.error) && (
          <p role="alert">部分演员资料加载失败，可点击上方刷新重试。</p>
        )}
        {view === "actors" && loaded.actors && (
          <Actors
            actors={actors}
            nonJavActors={nonJavActors}
            loadingActors={actorsQuery.isPending}
            loadingNonJavActors={nonJavQuery.isPending}
            busy={busy}
            prefs={prefs}
            onOpenWork={openWorkDetail}
            onOpenActor={openActorDetail}
            onActorTags={(actorKey, tags) => run(`tag-${actorKey}`, async () => {
              let next = await api.setActorTags({ actor_key: actorKey, ...tags });
              const existing = next.subscriptions.find((item) => item.actor_key === actorKey);
              if (tags.subscribe) {
                if (!existing) {
                  const actor = actors.find((item) => item.id === actorKey || item.name === actorKey);
                  const name = actor?.name ?? actorKey;
                  next = await api.upsertSubscription({
                    actor_key: actorKey,
                    actor_name: name,
                    start_date: new Date().toISOString().slice(0, 10),
                    max_cast: 3,
                    enabled: true
                  });
                } else if (!existing.enabled) {
                  next = await api.upsertSubscription({ ...existing, enabled: true });
                }
              } else if (existing) {
                next = await api.removeSubscription(actorKey);
              }
              setPrefs(next);
              setMessage(`已更新 ${actorKey} 标签`);
            }, "none")}
            saveNonJavActor={(previousName, payload) => run(`actor-${previousName ?? "new"}`, async () => {
              if (previousName) await api.updateNonJavActor(previousName, payload);
              else await api.createNonJavActor(payload);
              setMessage(previousName ? "演员资料已更新" : "演员已加入非 JAV 名单");
            }, "actors")}
            deleteNonJavActor={(actor) => run(`actor-delete-${actor.name}`, async () => {
              await api.deleteNonJavActor(actor.name);
              setMessage(`已从非 JAV 名单移除 ${actor.name}`);
            }, "actors")}
            uploadActorImage={(actor, file) => run(`actor-image-${actor.name}`, async () => {
              await api.uploadNonJavActorImage(actor.name, file);
              setMessage(`${actor.name} 的头像已更新`);
            }, "actors")}
          />
        )}

        {view === "actor-detail" && (
          <ActorDetailView
            model={selectedActorRef ? resolveActorDetail(selectedActorRef, actors, nonJavActors) : null}
            loading={actorsQuery.isPending || nonJavQuery.isPending}
            busy={busy}
            tags={(() => {
              if (!selectedActorRef) return undefined;
              const model = resolveActorDetail(selectedActorRef, actors, nonJavActors);
              if (!model) return undefined;
              const tags = prefs.actor_tags ?? {};
              return tags[model.tagKey] ?? tags[model.name] ?? { favorite: false, subscribe: false, blacklist: false };
            })()}
            onBack={closeActorDetail}
            onOpenWork={openWorkDetail}
            onFilterWorks={(name) => {
              navigateToFilteredWorks({ cast: name }, `已筛选演员作品：${name}`, true);
            }}
            report={setMessage}
            onActorTags={(actorKey, tags) => run(`tag-${actorKey}`, async () => {
              let next = await api.setActorTags({ actor_key: actorKey, ...tags });
              const existing = next.subscriptions.find((item) => item.actor_key === actorKey);
              if (tags.subscribe) {
                if (!existing) {
                  const actor = actors.find((item) => item.id === actorKey || item.name === actorKey);
                  const name = actor?.name ?? actorKey;
                  next = await api.upsertSubscription({
                    actor_key: actorKey,
                    actor_name: name,
                    start_date: new Date().toISOString().slice(0, 10),
                    max_cast: 3,
                    enabled: true
                  });
                } else if (!existing.enabled) {
                  next = await api.upsertSubscription({ ...existing, enabled: true });
                }
              } else if (existing) {
                next = await api.removeSubscription(actorKey);
              }
              setPrefs(next);
              setMessage(`已更新 ${actorKey} 标签`);
            }, "none")}
          />
        )}

        {view === "categories" && (
          <CategoriesView
            report={setMessage}
            onOpenTag={(tag) => {
              navigateToFilteredWorks({ tags: [tag] }, `已筛选标签：${tag}`, true);
            }}
          />
        )}

        {view === "works" && worksQuery.isPending && (
          <div className="skeleton-grid" aria-busy="true" aria-label="作品加载中">
            {Array.from({ length: 6 }, (_, index) => (
              <div className="skeleton-card" key={index}>
                <div className="skeleton-poster" />
                <div className="skeleton-lines">
                  <span /><span /><span />
                </div>
              </div>
            ))}
          </div>
        )}
        {view === "works" && loaded.works && (
          <Works
            works={works}
            busy={busy}
            filterEpoch={worksFilterEpoch}
            onOpenWork={openWorkDetail}
            onToggleWant={(work) => run(`want-${work.id}`, async () => {
              const next = await api.setWantList(work.id, !work.want_list);
              setPrefs(next);
              setWorks((current) => current.map((item) => (
                item.id === work.id ? { ...item, want_list: !work.want_list } : item
              )));
              setMessage(work.want_list ? "已移出想看" : "已加入想看");
            }, "none")}
            refreshMetadata={(work) => run(`work-${work.id}`, async () => {
              const result = await api.refreshWork(work.id);
              const failures = result.failures.map((failure) => failure.provider).join("、");
              setMessage(
                result.accepted_work_id
                  ? `已更新 ${work.primary_code ?? work.title} 的在线元数据`
                  : `未找到可自动接受的在线结果${failures ? `；失败来源：${failures}` : ""}`
              );
            }, "works")}
            lookupWork={(query) => run("lookup-work", async () => {
              const trimmed = query.trim();
              const looksLikeUrl = /^https?:\/\//i.test(trimmed);
              const result = await api.lookupWork(
                looksLikeUrl ? { source_url: trimmed } : { code: trimmed }
              );
              const failed = result.failures.map((item) => `${item.provider}/${item.reason}`).join("、");
              setMessage(
                result.work
                  ? `已按${looksLikeUrl ? "URL" : "番号"}建立或更新 ${result.work.primary_code ?? result.work.title}，聚合 ${result.matched_records} 个来源`
                  : `没有找到可自动确认的结果${result.matched_records ? `；有 ${result.matched_records} 条低置信候选未采用` : ""}${failed ? `；${failed}` : ""}`
              );
            }, "works")}
            translateWorks={() => run("translate-works", async () => {
              const result = await api.translateWorks();
              const error = result.errors.length > 0 ? `；${result.errors[0]}` : "";
              setMessage(`补翻译（标题/剧情）：成功 ${result.translated}，无需翻译 ${result.skipped}，失败 ${result.failed}，剩余 ${result.remaining}${error}`);
            }, "works")}
            seedCollections={() => run("seed-collections", async () => {
              const result = await api.seedCollections();
              setMessage(`合集索引：共 ${result.collections_total}，新建 ${result.collections_created}`);
            }, "works")}
          />
        )}

        {view === "work-detail" && selectedWorkId && (
          <WorkDetailView
            workId={selectedWorkId}
            busy={busy}
            onBack={closeWorkDetail}
            onOpenWork={openWorkDetail}
            report={setMessage}
            onSelectGenreTag={(tag) => {
              navigateToFilteredWorks({ tags: [tag] }, `已筛选标签：${tag}`, true);
            }}
            onSelectStudio={(studio) => {
              navigateToFilteredWorks({ studio }, `已筛选片商：${studio}`, true);
            }}
            onSelectSeries={(series) => {
              navigateToFilteredWorks({ q: series }, `已搜索系列：${series}`, true);
            }}
            onSelectYear={(year) => {
              navigateToFilteredWorks({ year }, `已筛选年份：${year}`, true);
            }}
            onSelectCategory={(category) => {
              navigateToFilteredWorks(
                { category: category as WorksFilterState["category"] },
                `已筛选分类：${category}`,
                true
              );
            }}
            onOpenActor={(name) => {
              const ref = findActorRefByName(name, actors, nonJavActors);
              if (ref) {
                if (!loaded.actors) void refreshActors();
                openActorDetail(ref);
                setMessage(`已打开演员：${name}`);
              } else {
                setSelectedWorkId(null);
                writeDetailToUrl({});
                setView("actors");
                setMessage(`未找到演员资料：${name}`);
              }
            }}
            onSave={(workId, payload) => run(`edit-${workId}`, async () => {
              await api.updateWork(workId, payload);
              setMessage("作品字段已保存（仅改 Work，来源快照不变）");
            }, "works")}
            onLocks={(workId, locks) => run(`locks-${workId}`, async () => {
              await api.updateWorkLocks(workId, locks);
              setMessage(`已更新字段锁：${locks.length ? locks.join("、") : "无"}`);
            }, "works")}
            onPreferPoster={(workId, index) => run(`poster-${workId}`, async () => {
              await api.preferWorkPoster(workId, index);
              setMessage("已选用缓存海报");
            }, "works")}
            onDeleteMagnet={(workId, magnetId) => run(`magnet-del-${magnetId}`, async () => {
              await api.deleteWorkMagnet(workId, magnetId);
              setMessage("已移除磁力链接");
            }, "none")}
            onRefresh={(workId) => {
              const work = works.find((item) => item.id === workId);
              if (!work) {
                void run(`work-${workId}`, async () => {
                  const result = await api.refreshWork(workId);
                  setMessage(result.accepted_work_id ? "已刷新元数据" : "未找到可自动接受的在线结果");
                }, "works");
                return;
              }
              void run(`work-${work.id}`, async () => {
                const result = await api.refreshWork(work.id);
                const failures = result.failures.map((failure) => failure.provider).join("、");
                setMessage(
                  result.accepted_work_id
                    ? `已更新 ${work.primary_code ?? work.title} 的在线元数据`
                    : `未找到可自动接受的在线结果${failures ? `；失败来源：${failures}` : ""}`
                );
              }, "works");
            }}
            onDownload={(workId) => {
              void run(`artwork-${workId}`, async () => {
                const result = await api.downloadArtwork(workId);
                setMessage(`图片缓存：新下载 ${result.downloaded}，已有 ${result.cached}，失败 ${result.failed}`);
              }, "works");
            }}
          />
        )}

        {view === "swipe" && (
          <SwipeFeedView
            works={works}
            loading={worksQuery.isPending}
            onLibraryMode={setSwipeNeedsWorks}
            onOpenDetail={openWorkDetail}
            report={setMessage}
          />
        )}

        {view === "rankings" && (
          <Suspense fallback={<p className="muted" aria-busy="true">正在打开页面…</p>}>
            <DiscoverPanel
              busy={busy}
              report={setMessage}
              onOpenWork={openWorkDetail}
              onFilterByCode={(code) => {
                navigateToFilteredWorks({ q: code }, `已按番号筛选：${code}`, true);
              }}
              wantListIds={prefs.want_list}
              onToggleWant={async (workId, wanted) => {
                await run(`want-${workId}`, async () => {
                  const next = await api.setWantList(workId, wanted);
                  setPrefs(next);
                  setWorks((items) =>
                    items.map((item) => (item.id === workId ? { ...item, want_list: wanted } : item))
                  );
                  setMessage(wanted ? "已加入想看" : "已移出想看");
                }, "none");
              }}
              onSeeded={async () => {
                await refreshWorks();
              }}
            />
          </Suspense>
        )}

        {view === "subscriptions" && (
          <Suspense fallback={<p className="muted" aria-busy="true">正在打开页面…</p>}>
            <SubscriptionsView
              prefs={prefs}
              busy={busy}
              report={setMessage}
              onChanged={setPrefs}
            />
          </Suspense>
        )}

        {view === "tasks" && (
          <section className="panel-section">
            <div className="section-hero compact">
              <div>
                <p className="eyebrow">OPS</p>
                <h1>任务</h1>
                <p className="muted">待确认收件箱与运行记录。手动下载类任务只进进度，不写入订阅。</p>
              </div>
            </div>
            <div className="sub-tabs">
              <button type="button" className={taskTab === "inbox" ? "active warm" : ""} onClick={() => setTaskTab("inbox")}>
                待确认 {inbox.length > 0 ? `(${inbox.length})` : ""}
              </button>
              <button type="button" className={taskTab === "runs" ? "active warm" : ""} onClick={() => setTaskTab("runs")}>
                运行记录
              </button>
            </div>
            {taskTab === "inbox" ? (
              <Inbox
                assets={inbox}
                libraries={libraries}
                nonJavActors={nonJavActors}
                candidates={candidates}
                busy={busy}
                identify={identify}
                createManualCandidate={createManualCandidate}
                loadCandidates={loadCandidates}
                accept={accept}
                assignDirectoryActor={(asset, actor, category, directory) => run(`directory-${asset.id}`, async () => {
                  const result = await api.assignDirectoryActor(asset.id, actor, category, directory);
                  setMessage(`目录 ${result.directory} 已绑定 ${result.actor}：处理 ${result.cataloged} 个，跳过 ${result.skipped} 个`);
                }, "all")}
                batchAccept={(ids) => run("batch-accept", async () => {
                  const result = await api.batchAcceptInbox(ids);
                  setMessage(`批量接受：成功 ${result.succeeded}，跳过 ${result.skipped}${result.errors[0] ? `；${result.errors[0]}` : ""}`);
                }, "all")}
                batchIgnore={(ids) => run("batch-ignore", async () => {
                  const result = await api.batchIgnoreInbox(ids);
                  setMessage(`批量标记跳过：成功 ${result.succeeded}，跳过 ${result.skipped}`);
                }, "inbox")}
                batchActor={(ids, actor, category) => run("batch-actor", async () => {
                  const result = await api.batchApplyActorInbox(ids, actor, category);
                  setMessage(`批量应用演员 ${actor}：成功 ${result.succeeded}，跳过 ${result.skipped}`);
                }, "all")}
              />
            ) : (
              <TaskCenter tasks={tasks} busy={busy} onChanged={async () => { await refreshTasks(); }} report={setMessage} onTasksSnapshot={setTasks} />
            )}
          </section>
        )}

        {view === "settings" && (
          <Suspense fallback={<p className="muted" aria-busy="true">正在打开页面…</p>}>
            <SettingsView
              libraries={libraries}
              busy={busy}
              run={(key, action, refresh = "core") => run(key, action, refresh)}
              report={setMessage}
            />
          </Suspense>
        )}
      </main>
    </div>
  );
}
