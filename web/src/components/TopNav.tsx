import { memo } from "react";

export type AppView = "actors" | "works" | "rankings" | "subscriptions" | "tasks" | "settings";

const ITEMS: ReadonlyArray<{ id: AppView; label: string; en: string; icon: string }> = [
  { id: "actors", label: "演员", en: "ACTORS", icon: "◇" },
  { id: "works", label: "影片", en: "LIBRARY", icon: "▣" },
  { id: "rankings", label: "榜单", en: "RANKINGS", icon: "◆" },
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
  return (
    <header className="top-nav">
      <div className="top-nav-brand">
        <span className="brand-mark">S</span>
        <div>
          <strong>Shadow MDC</strong>
          <small>local-first</small>
        </div>
      </div>
      <nav className="top-nav-items" aria-label="主导航">
        {ITEMS.map((item) => {
          const count = badges?.[item.id];
          return (
            <button
              key={item.id}
              type="button"
              className={view === item.id ? "top-nav-item active" : "top-nav-item"}
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
      <div className="live-pill" title="服务已连接">
        <span className="live-dot" />
        LIVE
      </div>
    </header>
  );
});
