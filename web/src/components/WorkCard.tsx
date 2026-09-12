import { memo } from "react";
import { appUrl } from "../api";
import type { Work } from "../model";
import { StatusBadges } from "./StatusBadges";

type Props = {
  work: Work;
  selected?: boolean;
  busy?: string | null;
  onSelect: (workId: string) => void;
  onRefresh?: (work: Work) => void;
  onToggleWant?: (work: Work) => void;
};

export const WorkCard = memo(function WorkCard({
  work,
  selected,
  busy,
  onSelect,
  onRefresh,
  onToggleWant
}: Props) {
  const thumb = work.image_url ? appUrl(work.image_url) : null;
  return (
    <article
      className={selected ? "work selected" : "work"}
      onClick={() => onSelect(work.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onSelect(work.id);
      }}
      role="button"
      tabIndex={0}
    >
      <div className="poster">
        {thumb ? (
          <img src={thumb} alt="" loading="lazy" decoding="async" />
        ) : (
          <div className="poster-fallback" aria-hidden />
        )}
        {work.primary_code && <span className="code-chip">{work.primary_code}</span>}
      </div>
      <div className="work-body">
        <span className="pill">{work.category}</span>
        <StatusBadges
          wantList={work.want_list}
          inCatalog
          hasLocalMedia={work.has_local_media}
        />
        <h2>{work.title}</h2>
        {work.original_title && work.original_title !== work.title && (
          <p className="original-title">原文：{work.original_title}</p>
        )}
        <p>{[work.studio, work.release_date].filter(Boolean).join(" · ")}</p>
        <div className="tags">
          {work.actor_entities.slice(0, 4).map((actor) => (
            <span key={actor.id}>{actor.name}</span>
          ))}
        </div>
        <div className="work-card-actions">
          {onToggleWant && (
            <button
              type="button"
              className="ghost work-refresh"
              disabled={busy === `want-${work.id}`}
              onClick={(event) => {
                event.stopPropagation();
                onToggleWant(work);
              }}
            >
              {work.want_list ? "取消想看" : "想看"}
            </button>
          )}
          {work.primary_code && onRefresh && (
            <button
              type="button"
              className="secondary work-refresh"
              disabled={busy === `work-${work.id}`}
              onClick={(event) => {
                event.stopPropagation();
                onRefresh(work);
              }}
            >
              刷新元数据
            </button>
          )}
        </div>
      </div>
    </article>
  );
});
