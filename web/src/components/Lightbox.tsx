import { useEffect, useRef, type KeyboardEvent } from "react";

/** Uncropped full-size image viewer with ←/→/Esc (idea from JavdbEmbySkin's preview modal). */
export function Lightbox({ urls, index, onIndex, onClose }: {
  urls: string[];
  index: number;
  onIndex: (next: number) => void;
  onClose: () => void;
}) {
  const count = urls.length;
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!count) return;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, [count > 0]);
  if (!count) return null;
  const currentIndex = Math.max(0, Math.min(index, count - 1));
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (["Escape", "ArrowRight", "ArrowLeft", "Tab"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
    }
    if (event.key === "Escape") onClose();
    else if (event.key === "ArrowRight") onIndex((currentIndex + 1) % count);
    else if (event.key === "ArrowLeft") onIndex((currentIndex - 1 + count) % count);
    else if (event.key === "Tab") {
      const buttons = [...(dialog.current?.querySelectorAll("button") ?? [])];
      const focused = buttons.findIndex((button) => button === document.activeElement);
      const next = focused < 0 ? (event.shiftKey ? buttons.length - 1 : 0)
        : (focused + (event.shiftKey ? buttons.length - 1 : 1)) % buttons.length;
      buttons[next]?.focus();
    }
  };
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label="图片预览"
      tabIndex={-1} ref={dialog} onKeyDown={onKey} onClick={onClose}>
      <button type="button" className="lightbox-close" aria-label="关闭预览"
        onClick={(event) => { event.stopPropagation(); onClose(); }}>×</button>
      <img src={urls[currentIndex]} alt={`预览 ${currentIndex + 1}`} onClick={(event) => event.stopPropagation()} />
      {count > 1 && (
        <>
          <button type="button" className="lightbox-nav prev" aria-label="上一张"
            onClick={(event) => { event.stopPropagation(); onIndex((currentIndex - 1 + count) % count); }}>‹</button>
          <button type="button" className="lightbox-nav next" aria-label="下一张"
            onClick={(event) => { event.stopPropagation(); onIndex((currentIndex + 1) % count); }}>›</button>
        </>
      )}
      <span className="lightbox-count">{currentIndex + 1} / {count}</span>
    </div>
  );
}
