import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import { DiscoverPanel } from "./components/DiscoverPanel";
import { TaskCenter } from "./components/TaskCenter";
import { TopNav, type AppView } from "./components/TopNav";
import type { ActorProfile, Asset, Candidate, Library, LibraryPrefs, NonJavActor, TaskRun, Work } from "./model";
import { Actors, Inbox, Works } from "./panels";
import { SettingsView } from "./views/SettingsView";
import { SubscriptionsView } from "./views/SubscriptionsView";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function App() {
  const [view, setView] = useState<AppView>("works");
  const [taskTab, setTaskTab] = useState<"inbox" | "runs">("runs");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [works, setWorks] = useState<Work[]>([]);
  const [actors, setActors] = useState<ActorProfile[]>([]);
  const [nonJavActors, setNonJavActors] = useState<NonJavActor[]>([]);
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [prefs, setPrefs] = useState<LibraryPrefs>({
    want_list: [],
    actor_tags: {},
    subscriptions: [],
    queue: []
  });
  const [candidates, setCandidates] = useState<Record<string, Candidate[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("正在连接 Shadow MDC…");
  const [loaded, setLoaded] = useState<Partial<Record<AppView | "core", boolean>>>({});

  const refreshCore = useCallback(async () => {
    const [nextLibraries, nextPrefs, nextTasks] = await Promise.all([
      api.libraries(),
      api.libraryPrefs(),
      api.tasks()
    ]);
    setLibraries(nextLibraries);
    setPrefs(nextPrefs);
    setTasks(nextTasks);
    setLoaded((current) => ({ ...current, core: true }));
  }, []);

  const refreshWorks = useCallback(async () => {
    setWorks(await api.works());
    setLoaded((current) => ({ ...current, works: true }));
  }, []);

  const refreshActors = useCallback(async () => {
    const [nextActors, nextNonJav] = await Promise.all([api.actors(), api.nonJavActors()]);
    setActors(nextActors);
    setNonJavActors(nextNonJav);
    setLoaded((current) => ({ ...current, actors: true }));
  }, []);

  const refreshInbox = useCallback(async () => {
    setAssets(await api.assets());
    setLoaded((current) => ({ ...current, tasks: true }));
  }, []);

  const refreshTasks = useCallback(async () => {
    setTasks(await api.tasks());
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        await refreshCore();
        setMessage("数据已同步");
      } catch (error) {
        setMessage(errorMessage(error));
      }
    })();
  }, [refreshCore]);

  useEffect(() => {
    void (async () => {
      try {
        let refreshed = false;
        if (view === "works" && !loaded.works) {
          await refreshWorks();
          refreshed = true;
        } else if (view === "actors" && !loaded.actors) {
          await refreshActors();
          refreshed = true;
        } else if (view === "tasks" && !loaded.tasks) {
          await refreshInbox();
          refreshed = true;
        }
        if (refreshed) setMessage("数据已同步");
      } catch (error) {
        setMessage(errorMessage(error));
      }
    })();
  }, [view, loaded.works, loaded.actors, loaded.tasks, refreshWorks, refreshActors, refreshInbox]);

  const inbox = useMemo(() => assets.filter((asset) => asset.state !== "identified"), [assets]);

  async function run(key: string, action: () => Promise<void>, refresh: "none" | "works" | "actors" | "inbox" | "tasks" | "core" | "all" = "all") {
    setBusy(key);
    try {
      await action();
      if (refresh === "works" || refresh === "all") await refreshWorks();
      if (refresh === "actors" || refresh === "all") await refreshActors();
      if (refresh === "inbox" || refresh === "all") await refreshInbox();
      if (refresh === "tasks" || refresh === "all") await refreshTasks();
      if (refresh === "core" || refresh === "all") await refreshCore();
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

  const badges = {
    tasks: inbox.length || undefined,
    subscriptions: prefs.queue.filter((item) => item.status === "pending").length || undefined
  };

  return (
    <div className="app-shell">
      <TopNav
        view={view}
        onChange={(next) => {
          setView(next);
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
                  setLoaded({});
                  await refreshCore();
                  if (view === "works") await refreshWorks();
                  if (view === "actors") await refreshActors();
                  if (view === "tasks") await refreshInbox();
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

        {view === "actors" && (
          <Actors
            actors={actors}
            nonJavActors={nonJavActors}
            busy={busy}
            prefs={prefs}
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

        {view === "works" && (
          <Works
            works={works}
            busy={busy}
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
            downloadArtwork={(work) => run(`artwork-${work.id}`, async () => {
              const result = await api.downloadArtwork(work.id);
              setMessage(`图片缓存：新下载 ${result.downloaded}，已有 ${result.cached}，失败 ${result.failed}`);
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
              setMessage(`标题翻译：成功 ${result.translated}，无需翻译 ${result.skipped}，失败 ${result.failed}，剩余 ${result.remaining}${error}`);
            }, "works")}
            saveWork={(workId, payload) => run(`edit-${workId}`, async () => {
              await api.updateWork(workId, payload);
              setMessage("作品字段已保存（仅改 Work，来源快照不变）");
            }, "works")}
            saveLocks={(workId, locks) => run(`locks-${workId}`, async () => {
              await api.updateWorkLocks(workId, locks);
              setMessage(`已更新字段锁：${locks.length ? locks.join("、") : "无"}`);
            }, "works")}
            preferPoster={(workId, index) => run(`poster-${workId}`, async () => {
              await api.preferWorkPoster(workId, index);
              setMessage("已选用缓存海报");
            }, "works")}
            deleteMagnet={(workId, magnetId) => run(`magnet-del-${magnetId}`, async () => {
              await api.deleteWorkMagnet(workId, magnetId);
              setMessage("已移除磁力链接");
            }, "none")}
            seedCollections={() => run("seed-collections", async () => {
              const result = await api.seedCollections();
              setMessage(`合集索引：共 ${result.collections_total}，新建 ${result.collections_created}`);
            }, "works")}
          />
        )}

        {view === "rankings" && (
          <DiscoverPanel
            busy={busy}
            report={setMessage}
            onSeeded={async () => {
              await refreshWorks();
            }}
          />
        )}

        {view === "subscriptions" && (
          <SubscriptionsView
            prefs={prefs}
            busy={busy}
            report={setMessage}
            onChanged={setPrefs}
          />
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
          <SettingsView
            libraries={libraries}
            busy={busy}
            run={(key, action, refresh = "core") => run(key, action, refresh)}
            report={setMessage}
          />
        )}
      </main>
    </div>
  );
}
