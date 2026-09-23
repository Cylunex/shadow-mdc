import { useEffect, useMemo } from "react";

import { appUrl } from "../api";
import { hideBrokenImage, mediaUrl } from "../lib/mediaUrl";
import { XHandleLink } from "../components/XHandleLink";
import type { ActorJavRankingInfo, ActorProfile, NonJavActor } from "../model";

export type ActorDetailSource = "jav" | "non-jav";

export type ActorWorkRef = {
  id: string;
  title: string;
  code: string | null;
  image_url: string | null;
  studio?: string | null;
  series?: string | null;
  category?: string;
};

export type ActorDetailModel = {
  ref: string;
  source: ActorDetailSource;
  name: string;
  aliases: string[];
  categories: string[];
  work_count: number;
  works: ActorWorkRef[];
  image_url: string | null;
  x_handle?: string | null;
  x_url?: string | null;
  biography?: string | null;
  notes?: string | null;
  groups?: string[];
  javranking?: ActorJavRankingInfo | null;
  tagKey: string;
};

export function javActorRef(actor: Pick<ActorProfile, "id" | "name">): string {
  return `jav:${actor.id ?? actor.name}`;
}

export function nonJavActorRef(actor: Pick<NonJavActor, "name">): string {
  return `nj:${actor.name}`;
}

export function parseActorRef(ref: string): { source: ActorDetailSource; key: string } | null {
  if (ref.startsWith("jav:")) return { source: "jav", key: ref.slice(4) };
  if (ref.startsWith("nj:")) return { source: "non-jav", key: ref.slice(3) };
  // legacy / bare name fallback
  if (ref.trim()) return { source: "jav", key: ref.trim() };
  return null;
}

export function resolveActorDetail(
  ref: string,
  actors: ActorProfile[],
  nonJavActors: NonJavActor[]
): ActorDetailModel | null {
  const parsed = parseActorRef(ref);
  if (!parsed) return null;
  if (parsed.source === "jav") {
    const actor = actors.find((item) => item.id === parsed.key || item.name === parsed.key);
    if (!actor) return null;
    return {
      ref: javActorRef(actor),
      source: "jav",
      name: actor.name,
      aliases: actor.aliases,
      categories: actor.categories,
      work_count: actor.work_count,
      works: actor.works.map((work) => ({
        id: work.id,
        title: work.title,
        code: work.code,
        image_url: work.image_url
      })),
      image_url: actor.image_url ?? null,
      x_handle: actor.x_handle,
      x_url: (actor as { x_url?: string | null }).x_url,
      javranking: actor.javranking ?? null,
      tagKey: actor.id ?? actor.name
    };
  }
  const actor = nonJavActors.find((item) => item.name === parsed.key);
  if (!actor) return null;
  return {
    ref: nonJavActorRef(actor),
    source: "non-jav",
    name: actor.name,
    aliases: actor.aliases,
    categories: actor.categories,
    work_count: actor.work_count,
    works: actor.works.map((work) => ({
      id: work.id,
      title: work.title,
      code: work.code,
      image_url: work.image_url,
      studio: work.studio,
      series: work.series,
      category: work.category
    })),
    image_url: actor.image_url,
    x_handle: actor.x_handle,
    x_url: actor.x_url,
    biography: actor.biography,
    notes: actor.notes,
    groups: actor.groups,
    tagKey: actor.name
  };
}

export function findActorRefByName(
  name: string,
  actors: ActorProfile[],
  nonJavActors: NonJavActor[]
): string | null {
  const trimmed = name.trim();
  if (!trimmed) return null;
  const jav = actors.find(
    (item) => item.name === trimmed || item.aliases.includes(trimmed) || item.id === trimmed
  );
  if (jav) return javActorRef(jav);
  const nj = nonJavActors.find(
    (item) => item.name === trimmed || item.aliases.includes(trimmed) || item.match_names.includes(trimmed)
  );
  if (nj) return nonJavActorRef(nj);
  return `jav:${trimmed}`;
}

type TagState = { favorite: boolean; subscribe: boolean; blacklist: boolean };

export function ActorDetailView(props: {
  model: ActorDetailModel | null;
  loading?: boolean;
  busy: string | null;
  tags?: TagState;
  onBack: () => void;
  onOpenWork: (workId: string) => void;
  onActorTags?: (actorKey: string, tags: TagState) => Promise<void>;
  report?: (message: string) => void;
}) {
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [props.model?.ref]);

  const portrait = useMemo(() => {
    if (!props.model?.image_url) return null;
    return mediaUrl(props.model.image_url);
  }, [props.model?.image_url]);

  if (props.loading && !props.model) {
    return (
      <section className="actor-page">
        <div className="work-page-toolbar">
          <button type="button" className="ghost" onClick={props.onBack}>← 返回演员列表</button>
        </div>
        <p className="muted">正在加载演员详情…</p>
      </section>
    );
  }

  if (!props.model) {
    return (
      <section className="actor-page">
        <div className="work-page-toolbar">
          <button type="button" className="ghost" onClick={props.onBack}>← 返回演员列表</button>
        </div>
        <div className="empty">
          <div>!</div>
          <h2>无法打开演员</h2>
          <p>演员不存在或资料尚未同步</p>
        </div>
      </section>
    );
  }

  const actor = props.model;
  const tag = props.tags ?? { favorite: false, subscribe: false, blacklist: false };
  const tagBusy = props.busy === `tag-${actor.tagKey}`;

  return (
    <section className="actor-page">
      <div className="work-page-toolbar">
        <button type="button" className="ghost" onClick={props.onBack}>← 返回演员列表</button>
        <div className="work-page-toolbar-actions">
          <span className="pill">{actor.source === "jav" ? "JAV" : "非 JAV"}</span>
          <span className="muted">作品 {actor.work_count}</span>
        </div>
      </div>

      <div className="actor-page-scroll">
        <header className="actor-page-hero work-page-hero">
          <div className="actor-page-portrait-wrap">
            {portrait ? (
              <img className="actor-page-portrait" src={portrait} alt={`${actor.name} 写真`} onError={hideBrokenImage} />
            ) : (
              <div className="actor-page-portrait actor-page-portrait--empty" aria-label="暂无写真" />
            )}
          </div>
          <div className="work-page-hero-meta">
            <p className="eyebrow">{actor.categories.join(" / ") || "ACTOR"}</p>
            <h1>{actor.name}</h1>
            {actor.aliases.length > 0 && (
              <p className="actor-page-aliases">别名：{actor.aliases.join("、")}</p>
            )}
            {actor.groups && actor.groups.length > 0 && (
              <p className="muted">分组：{actor.groups.join(" / ")}</p>
            )}
            <XHandleLink handle={actor.x_handle} url={actor.x_url} />

            {actor.source === "jav" && props.onActorTags && (
              <div className="actor-tag-actions actor-page-tags">
                <button
                  type="button"
                  className={tag.favorite ? "active" : "ghost"}
                  disabled={tagBusy}
                  onClick={() => void props.onActorTags?.(actor.tagKey, { ...tag, favorite: !tag.favorite, blacklist: false })}
                >收藏</button>
                <button
                  type="button"
                  className={tag.subscribe ? "active" : "ghost"}
                  disabled={tagBusy}
                  onClick={() => void props.onActorTags?.(actor.tagKey, { ...tag, subscribe: !tag.subscribe, blacklist: false })}
                >订阅</button>
                <button
                  type="button"
                  className={tag.blacklist ? "danger" : "ghost"}
                  disabled={tagBusy}
                  onClick={() => void props.onActorTags?.(actor.tagKey, { favorite: false, subscribe: false, blacklist: !tag.blacklist })}
                >黑名单</button>
              </div>
            )}

            {actor.javranking && (
              <div className="javranking-honors actor-javranking">
                <div className="javranking-honors-head">
                  <h3>JavRanking 战力</h3>
                  {actor.javranking.compact_badge && (
                    <a
                      className="javranking-badge compact"
                      href={actor.javranking.honors[0]?.url || "#"}
                      target="_blank"
                      rel="noreferrer"
                    >{actor.javranking.compact_badge}</a>
                  )}
                </div>
                <div className="tags javranking-badges">
                  {(actor.javranking.honors ?? []).map((honor) => (
                    <a
                      key={`${honor.list_slug}-${honor.position}`}
                      className="javranking-badge"
                      href={honor.url || "#"}
                      target="_blank"
                      rel="noreferrer"
                    >{honor.label}</a>
                  ))}
                </div>
              </div>
            )}

            {actor.biography && <p className="actor-biography">{actor.biography}</p>}
            {actor.notes && <p className="actor-notes">备注：{actor.notes}</p>}
          </div>
        </header>

        <section className="work-page-section actor-page-works">
          <div className="work-related-head">
            <h2>作品</h2>
            <span className="muted">{actor.works.length}</span>
          </div>
          {actor.works.length === 0 ? (
            <p className="muted">暂无关联作品</p>
          ) : (
            <div className="actor-page-work-grid" role="list">
              {actor.works.map((work) => {
                const thumb = mediaUrl(work.image_url);
                const codeLabel = work.code ?? work.studio ?? work.series ?? null;
                return (
                  <button
                    key={work.id}
                    type="button"
                    className="actor-page-work-card"
                    role="listitem"
                    title={work.title}
                    onClick={() => props.onOpenWork(work.id)}
                  >
                    <div className="actor-page-work-poster">
                      {thumb ? (
                        <img src={thumb} alt="" loading="lazy" decoding="async" onError={hideBrokenImage} />
                      ) : (
                        <div className="poster-fallback" aria-hidden />
                      )}
                    </div>
                    <div className="actor-page-work-meta">
                      {codeLabel && (
                        <strong
                          className={work.code ? "code-chip-copyable" : undefined}
                          title={work.code ? "点击复制番号" : undefined}
                          onClick={work.code ? (event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            void navigator.clipboard.writeText(work.code!).then(
                              () => props.report?.("已复制番号"),
                              () => props.report?.("复制番号失败")
                            );
                          } : undefined}
                        >{codeLabel}</strong>
                      )}
                      <span>{work.title}</span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </section>
  );
}
