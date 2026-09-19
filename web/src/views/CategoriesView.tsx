import { useEffect, useMemo, useState } from "react";

import { api, appUrl } from "../api";
import type { CategoryItem } from "../model";

type Props = {
  onOpenTag: (tag: string) => void;
  report?: (message: string) => void;
};

export function CategoriesView({ onOpenTag, report }: Props) {
  const [items, setItems] = useState<CategoryItem[]>([]);
  const [onlyWithWorks, setOnlyWithWorks] = useState(false);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const page = await api.categories({ onlyWithWorks });
        if (!cancelled) {
          setItems(page.categories);
          report?.(`分类 ${page.categories.length} 个`);
        }
      } catch (error) {
        if (!cancelled) {
          report?.(error instanceof Error ? error.message : String(error));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [onlyWithWorks, report]);

  const visible = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    if (!q) return items;
    return items.filter((item) => {
      const hay = [item.label, item.slug, ...(item.aliases ?? [])].join(" ").toLocaleLowerCase();
      return hay.includes(q);
    });
  }, [items, query]);

  return (
    <section className="panel-section categories-view">
      <div className="section-hero compact">
        <div>
          <p className="eyebrow">CATEGORIES</p>
          <h1>分类</h1>
          <p className="muted">封面分类墙 · 点击进入影片并套用标签筛选</p>
        </div>
        <div className="categories-toolbar">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索分类…"
            aria-label="搜索分类"
          />
          <label className="categories-toggle">
            <input
              type="checkbox"
              checked={onlyWithWorks}
              onChange={(event) => setOnlyWithWorks(event.target.checked)}
            />
            仅显示有作品
          </label>
          <span className="muted">{loading ? "加载中…" : `${visible.length} / ${items.length}`}</span>
        </div>
      </div>

      {visible.length === 0 && !loading ? (
        <div className="empty">
          <div>▣</div>
          <h2>暂无分类</h2>
          <p>请先运行 scripts/seed_category_covers.py 写入封面清单</p>
        </div>
      ) : (
        <div className="category-grid" aria-label="分类网格">
          {visible.map((item) => (
            <button
              key={item.slug}
              type="button"
              className="category-card"
              onClick={() => onOpenTag(item.label)}
              title={`按「${item.label}」筛选影片`}
            >
              <div className="category-cover">
                {item.image_url ? (
                  <img src={appUrl(item.image_url) ?? item.image_url ?? undefined} alt="" loading="lazy" decoding="async" />
                ) : (
                  <div className="category-cover-fallback" aria-hidden />
                )}
                <span className="category-count">{item.work_count}</span>
              </div>
              <div className="category-meta">
                <strong>{item.label}</strong>
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
