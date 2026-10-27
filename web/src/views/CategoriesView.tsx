import { useQuery } from "@tanstack/react-query";
import { CoverImage } from "../components/CoverImage";
import { useEffect, useMemo, useState } from "react";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

import { appUrl } from "../api";

type Props = {
  onOpenTag: (tag: string) => void;
  report?: (message: string) => void;
};

export function CategoriesView({ onOpenTag, report }: Props) {
  const [onlyWithWorks, setOnlyWithWorks] = useState(false);
  const [query, setQuery] = useState("");

  const result = useQuery(resources.categories(onlyWithWorks), queryClient);
  const items = result.data?.categories ?? [];
  const loading = result.isPending;
  useEffect(() => {
    if (result.error) report?.(result.error.message);
    else if (result.data) report?.(`分类 ${result.data.categories.length} 个`);
  }, [result.data, result.error, report]);

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

      {result.error && !result.data ? (
        <p role="alert">分类加载失败，可点击上方刷新重试。</p>
      ) : loading ? (
        <div className="skeleton-grid" aria-busy="true" aria-label="分类加载中">
          {Array.from({ length: 8 }, (_, index) => (
            <div className="skeleton-card" key={index}>
              <div className="skeleton-poster" />
              <div className="skeleton-lines"><span /><span /></div>
            </div>
          ))}
        </div>
      ) : visible.length === 0 ? (
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
                  <CoverImage src={appUrl(item.image_url) ?? item.image_url} />
                ) : (
                  <div className="category-cover-fallback" aria-hidden />
                )}
              </div>
              <div className="category-meta">
                <strong>{item.label}</strong>
                <span className="category-count">{item.work_count} 部</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
