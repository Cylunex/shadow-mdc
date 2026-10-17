import { memo, useMemo } from "react";

import { CoverImage } from "./CoverImage";
import { mediaUrl } from "../lib/mediaUrl";

export type ActorWorkTile = {
  id: string;
  title: string;
  code?: string | null;
  image_url?: string | null;
  release_date?: string | null;
  studio?: string | null;
};

type Props = {
  works: ActorWorkTile[];
  /** How many covers to show before the 「+N」 tile. */
  limit?: number;
  total?: number;
  onOpenWork: (workId: string) => void;
  onOpenAll?: () => void;
  onCopyCode?: (code: string) => void;
};

function yearLabel(value?: string | null): string | null {
  if (!value) return null;
  const match = /^(\d{4})/.exec(value);
  return match ? match[1]! : null;
}

/** Horizontal cover strip for an actor's works (wide 16:9 covers, not tall slivers). */
export const ActorWorkStrip = memo(function ActorWorkStrip({
  works,
  limit = 6,
  total,
  onOpenWork,
  onOpenAll,
  onCopyCode
}: Props) {
  const visible = works.slice(0, limit);
  const remaining = Math.max(0, (total ?? works.length) - visible.length);
  if (visible.length === 0) {
    return <p className="actor-work-strip-empty">暂无关联作品</p>;
  }
  return (
    <div className="actor-work-strip" role="list">
      {visible.map((work) => {
        const cover = mediaUrl(work.image_url);
        const year = yearLabel(work.release_date);
        return (
          <button
            key={work.id}
            type="button"
            role="listitem"
            className="actor-work-tile"
            title={[work.code, work.title].filter(Boolean).join(" · ")}
            onClick={() => onOpenWork(work.id)}
          >
            <span className="actor-work-tile-cover">
              {cover ? <CoverImage src={cover} /> : <span className="poster-fallback" aria-hidden />}
              {work.code ? (
                <span
                  className={onCopyCode ? "actor-work-tile-code code-chip-copyable" : "actor-work-tile-code"}
                  title={onCopyCode ? "点击复制番号" : undefined}
                  onClick={
                    onCopyCode
                      ? (event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          onCopyCode(work.code!);
                        }
                      : undefined
                  }
                >
                  {work.code}
                </span>
              ) : null}
            </span>
            <span className="actor-work-tile-title">{work.title}</span>
            {work.studio || year ? (
              <span className="actor-work-tile-meta">
                {[year, work.studio].filter(Boolean).join(" · ")}
              </span>
            ) : null}
          </button>
        );
      })}
      {remaining > 0 && (
        <button
          type="button"
          role="listitem"
          className="actor-work-tile actor-work-tile--more"
          title={`还有 ${remaining} 部作品`}
          onClick={() => (onOpenAll ? onOpenAll() : undefined)}
        >
          <span className="actor-work-tile-cover actor-work-tile-cover--more">
            <strong>+{remaining}</strong>
            <small>更多作品</small>
          </span>
        </button>
      )}
    </div>
  );
});

/** One-line actor context: how many works, newest release, dominant studio. */
export function ActorWorkSummary({
  works,
  workCount
}: {
  works: Array<{ release_date?: string | null; studio?: string | null }>;
  workCount?: number;
}) {
  const stats = useMemo(() => {
    let newest: string | null = null;
    const studios = new Map<string, number>();
    for (const work of works) {
      const date = work.release_date ?? null;
      if (date && (!newest || date > newest)) newest = date;
      const studio = (work.studio ?? "").trim();
      if (studio) studios.set(studio, (studios.get(studio) ?? 0) + 1);
    }
    const top = [...studios.entries()].sort((left, right) => right[1] - left[1])[0]?.[0] ?? null;
    return { newest: newest ? newest.slice(0, 7) : null, top };
  }, [works]);
  const parts: string[] = [];
  if (typeof workCount === "number" && workCount > 0) parts.push(`作品 ${workCount}`);
  if (stats.newest) parts.push(`最新 ${stats.newest}`);
  if (stats.top) parts.push(`常驻 ${stats.top}`);
  if (parts.length === 0) return null;
  return <p className="actor-work-summary">{parts.join(" · ")}</p>;
}
