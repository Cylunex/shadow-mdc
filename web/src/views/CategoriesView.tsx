import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { z } from "zod";
import { appUrl } from "../api";
import { CoverImage } from "../components/CoverImage";
import { Pagination } from "../components/Pagination";
import { useSessionState } from "../hooks/useSessionState";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

const browsingSchema = z.object({
  query: z.string(), onlyWithWorks: z.boolean(), sort: z.enum(["featured", "count", "name"]),
  page: z.number().int().min(1), pageSize: z.union([z.literal(24), z.literal(48), z.literal(96)]),
});
const defaults: z.infer<typeof browsingSchema> = { query: "", onlyWithWorks: false, sort: "featured", page: 1, pageSize: 24 };

type Props = {
  onOpenTag: (id: string, name?: string) => void;
  report?: (message: string) => void;
};

export function CategoriesView({ onOpenTag, report }: Props) {
  const [browsing, setBrowsing] = useSessionState("mdc:category-browsing", browsingSchema, defaults);
  const { query, onlyWithWorks, sort, pageSize } = browsing;
  const heading = useRef<HTMLDivElement>(null);
  const result = useQuery(resources.categories(), queryClient);
  const loading = result.isPending;
  useEffect(() => {
    if (result.error) report?.(result.error.message);
    else if (result.data) report?.(`分类 ${result.data.categories.length} 个`);
  }, [result.data, result.error, report]);

  const visible = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const items = (result.data?.categories ?? []).filter(item =>
      (!onlyWithWorks || item.work_count > 0) && (!q ||
        [item.id, item.label, item.slug, ...item.aliases].join(" ").toLocaleLowerCase().includes(q)));
    if (sort === "count") items.sort((a, b) => b.work_count - a.work_count || a.label.localeCompare(b.label, "zh-CN"));
    if (sort === "name") items.sort((a, b) => a.label.localeCompare(b.label, "zh-CN"));
    return items;
  }, [result.data, query, onlyWithWorks, sort]);
  const page = Math.min(browsing.page, Math.max(1, Math.ceil(visible.length / pageSize)));
  useEffect(() => {
    if (result.data && page !== browsing.page) setBrowsing(previous => ({ ...previous, page }));
  }, [result.data, page, browsing.page, setBrowsing]);
  const items = visible.slice((page - 1) * pageSize, page * pageSize);
  function changePage(next: number) {
    setBrowsing(previous => ({ ...previous, page: next }));
    heading.current?.scrollIntoView?.({ block: "start" });
  }
  const pagination = {
    label: "分类", page, pageSize, total: visible.length, busy: result.isFetching,
    onPageChange: changePage,
    onPageSizeChange: (size: number) => setBrowsing(previous => ({ ...previous, page: 1, pageSize: size as 24 | 48 | 96 })),
  };
  const filtered = Boolean(query.trim() || onlyWithWorks);
  return <section className="panel-section categories-view">
    <div className="catalog-heading" ref={heading}>
      <div><p className="eyebrow">CATEGORIES</p><div className="category-heading-title"><h1>分类浏览</h1>
        <span className="muted">{loading ? "正在读取分类…" : `${visible.length} / ${result.data?.categories.length ?? 0} 个分类`}</span>
      </div><p className="muted">按主题标签浏览作品，选择图卡查看对应影片。</p></div>
    </div>
    <div className="categories-toolbar">
      <input id="shadow-mdc-search" value={query} onChange={event => setBrowsing(previous => ({ ...previous, query: event.target.value, page: 1 }))}
        placeholder="分类名称、别名…" aria-label="搜索分类" maxLength={200} />
      <select aria-label="分类排序" value={sort} onChange={event => setBrowsing(previous => ({
        ...previous, sort: event.target.value as typeof sort, page: 1,
      }))}>
        <option value="featured">推荐顺序</option><option value="count">作品最多</option><option value="name">分类名称</option>
      </select>
      <label className="categories-toggle"><input type="checkbox" checked={onlyWithWorks}
        onChange={event => setBrowsing(previous => ({ ...previous, onlyWithWorks: event.target.checked, page: 1 }))} />仅显示有作品</label>
      {filtered && <button className="ghost" onClick={() => setBrowsing(previous => ({ ...previous, query: "", onlyWithWorks: false, page: 1 }))}>清除筛选</button>}
    </div>
    {result.error && <div className="catalog-error" role="alert">
      <span>分类加载失败：{result.error.message}</span>
      <button className="ghost" disabled={result.isFetching} onClick={() => void result.refetch()}>重试分类</button>
    </div>}
    {loading ? <div className="skeleton-grid" aria-busy="true" aria-label="分类加载中">
      {Array.from({ length: 8 }, (_, index) => <div className="skeleton-card" key={index}>
        <div className="skeleton-poster" /><div className="skeleton-lines"><span /><span /></div>
      </div>)}
    </div> : result.data && <>
      <Pagination {...pagination} />
      {visible.length === 0 ? <div className="empty">
        <h2>{filtered ? "没有匹配的分类" : "暂无分类"}</h2>
        <p>{filtered ? "试试其他关键词，或清除筛选查看全部分类。" : "作品入库后，可在这里按分类浏览。"}</p>
      </div> : <div className="category-grid" aria-label="分类网格" aria-busy={result.isFetching}>
        {items.map(item => <button key={item.id ?? item.label} type="button" className="category-card"
          onClick={() => onOpenTag(item.id ?? item.label, item.label)} title={`查看「${item.label}」的 ${item.work_count} 部作品`}>
          <div className="category-cover"><CoverImage src={appUrl(item.image_url) ?? ""} />
            {!item.image_url && <span className="category-cover-label" aria-hidden="true">{item.label}</span>}
          </div>
          <div className="category-meta"><strong>{item.label}</strong><span className="category-count">{item.work_count} 部作品</span></div>
        </button>)}
      </div>}
      {visible.length > pageSize && <Pagination {...pagination} position="底部" />}
    </>}
  </section>;
}
