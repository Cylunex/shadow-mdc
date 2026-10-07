import { useEffect } from "react";

/** Uncropped full-size image viewer with ←/→/Esc (idea from JavdbEmbySkin's preview modal). */
export function Lightbox({ urls, index, onIndex, onClose }: {
  urls: string[];
  index: number;
  onIndex: (next: number) => void;
  onClose: () => void;
}) {
  const count = urls.length;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      else if (event.key === "ArrowRight") onIndex((index + 1) % count);
      else if (event.key === "ArrowLeft") onIndex((index - 1 + count) % count);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, count, onIndex, onClose]);
  if (!count) return null;
  return (
    <div className="lightbox" role="dialog" aria-modal="true" onClick={onClose}>
      <img src={urls[index]} alt="" onClick={(event) => event.stopPropagation()} />
      {count > 1 && (
        <>
          <button type="button" className="lightbox-nav prev" aria-label="上一张"
            onClick={(event) => { event.stopPropagation(); onIndex((index - 1 + count) % count); }}>‹</button>
          <button type="button" className="lightbox-nav next" aria-label="下一张"
            onClick={(event) => { event.stopPropagation(); onIndex((index + 1) % count); }}>›</button>
        </>
      )}
      <span className="lightbox-count">{index + 1} / {count}</span>
    </div>
  );
}
