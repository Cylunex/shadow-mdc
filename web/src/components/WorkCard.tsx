import { CoverImage } from "./CoverImage";
import { memo } from "react";
import { mediaUrl } from "../lib/mediaUrl";
import type { Work } from "../model";
import { StatusBadges } from "./StatusBadges";

type Props = {
  work: Work;
  selected?: boolean;
  busy?: string | null;
  /** Client search match score (0–100); shown when searching. */
  matchScore?: number;
  onSelect: (workId: string) => void;
  onRefresh?: (work: Work) => void;
  onToggleWant?: (work: Work) => void;
  onFilterStudio?: (studio: string) => void;
  onFilterCast?: (name: string) => void;
};

function matchRankLabel(score: number): string {
  if (score >= 90) return "番号命中";
  if (score >= 70) return "精确匹配";
  if (score >= 50) return "高相关";
  if (score >= 35) return "相关";
  return "弱相关";
}

export const WorkCard = memo(function WorkCard({
  work,
  selected,
  busy,
  matchScore,
  onSelect,
  onRefresh,
  onToggleWant,
  onFilterStudio,
  onFilterCast
}: Props) {
  const thumb = mediaUrl(work.image_url);
  const showRank = matchScore != null && matchScore > 0;
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
          <CoverImage src={thumb} />
        ) : (
          <div className="poster-fallback" aria-hidden />
        )}
        {showRank && (
          <span className="match-rank-badge" title={`匹配分 ${matchScore}`}>
            {matchRankLabel(matchScore)} · {matchScore}
          </span>
        )}
      </div>
      <div className="work-body">
        <div className="work-code-row">
            {work.primary_code && (
              <span
                className="work-code code-chip-copyable"
                title="点击复制番号"
                role="button"
                onClick={(event) => {
                  event.stopPropagation();
                  void navigator.clipboard.writeText(work.primary_code!).catch(() => undefined);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    event.stopPropagation();
                    void navigator.clipboard.writeText(work.primary_code!).catch(() => undefined);
                  }
                }}
              >{work.primary_code}</span>
            )}
            {work.rating_value != null && (
              <span className="work-score" title={work.rating_source ?? undefined}>
                ★ {work.rating_value}
              </span>
            )}
        </div>
        <span className="pill">{work.category}</span>
        <StatusBadges
          wantList={work.want_list}
          hasLocalMedia={work.has_local_media}
        />
        <h2>{work.title}</h2>
        {work.original_title && work.original_title !== work.title && (
          <p className="original-title">原文：{work.original_title}</p>
        )}
        <p className="work-meta-line">
          {work.studio ? (
            onFilterStudio ? (
              <button
                type="button"
                className="meta-link-chip"
                title={`按片商「${work.studio}」筛选`}
                onClick={(event) => {
                  event.stopPropagation();
                  onFilterStudio(work.studio!);
                }}
              >{work.studio}</button>
            ) : work.studio
          ) : null}
          {work.studio && work.release_date ? " · " : null}
          {work.release_date ?? null}
        </p>
        <div className="tags">
          {(work.actor_entities.length > 0
            ? work.actor_entities.slice(0, 4).map((actor) => (
              onFilterCast ? (
                <button
                  key={actor.id}
                  type="button"
                  className="meta-link-chip"
                  title={`按演员「${actor.name}」筛选`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onFilterCast(actor.name);
                  }}
                >{actor.name}</button>
              ) : (
                <span key={actor.id}>{actor.name}</span>
              )
            ))
            : work.actors.slice(0, 4).map((name) => (
              onFilterCast ? (
                <button
                  key={name}
                  type="button"
                  className="meta-link-chip"
                  title={`按演员「${name}」筛选`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onFilterCast(name);
                  }}
                >{name}</button>
              ) : (
                <span key={name}>{name}</span>
              )
            )))}
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
