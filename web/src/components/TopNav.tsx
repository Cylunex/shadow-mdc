import { memo, useEffect, useRef } from "react";

export type AppView = "actors" | "actor-detail" | "works" | "work-detail" | "categories" | "rankings" | "swipe" | "tvbox" | "subscriptions" | "tasks" | "settings";

const ITEMS: ReadonlyArray<{ id: AppView; label: string; en: string; icon: string }> = [
  { id: "actors", label: "演员", en: "ACTORS", icon: "◇" },
  { id: "works", label: "影片", en: "LIBRARY", icon: "▣" },
  { id: "categories", label: "分类", en: "CATEGORIES", icon: "▤" },
  { id: "rankings", label: "榜单", en: "RANKINGS", icon: "◆" },
  { id: "swipe", label: "刷流", en: "SWIPE", icon: "▽" },
  { id: "tvbox", label: "TVBox", en: "ONLINE", icon: "▷" },
  { id: "subscriptions", label: "订阅", en: "QUEUE", icon: "✦" },
  { id: "tasks", label: "任务", en: "OPS", icon: "▥" },
  { id: "settings", label: "设置", en: "SYSTEM", icon: "⚙" }
];

type Props = {
  view: AppView;
  onChange: (view: AppView) => void;
  badges?: Partial<Record<AppView, number>>;
};

export const TopNav = memo(function TopNav({ view, onChange, badges }: Props) {
  const current = view === "work-detail" ? "works" : view === "actor-detail" ? "actors" : view;
  const nav = useRef<HTMLElement>(null);
  useEffect(() => {
    const bar = nav.current;
    const item = bar?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!bar || !item || bar.scrollWidth <= bar.clientWidth) return;
    const bounds = bar.getBoundingClientRect();
    const active = item.getBoundingClientRect();
    if (active.left < bounds.left) bar.scrollLeft += active.left - bounds.left;
    else if (active.right > bounds.right) bar.scrollLeft += active.right - bounds.right;
  }, [current]);
  return (
    <header className="top-nav">
      <div className="top-nav-brand">
        <span className="brand-mark">S</span>
        <div>
          <strong>Shadow MDC</strong>
          <small>local-first</small>
        </div>
      </div>
      <nav ref={nav} className="top-nav-items" aria-label="主导航">
        {ITEMS.map((item) => {
          const count = badges?.[item.id];
          return (
            <button
              key={item.id}
              type="button"
              aria-current={current === item.id ? "page" : undefined}
              className={current === item.id ? "top-nav-item active" : "top-nav-item"}
              onClick={() => onChange(item.id)}
            >
              <span className="top-nav-icon" aria-hidden>{item.icon}</span>
              <span className="top-nav-label">
                <b>{item.label}</b>
                <small>{item.en}</small>
              </span>
              {typeof count === "number" && count > 0 && <em className="nav-badge">{count}</em>}
            </button>
          );
        })}
      </nav>
    </header>
  );
});
