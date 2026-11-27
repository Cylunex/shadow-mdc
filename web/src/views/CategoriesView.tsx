import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";
import { appUrl } from "../api";
import { CoverImage } from "../components/CoverImage";
import { Pagination } from "../components/Pagination";
import { useSessionState } from "../hooks/useSessionState";
import { tagTreeRows } from "../lib/tagTree";
import type { CategoryItem } from "../model";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

const browsingSchema = z.object({
  query: z.string(), onlyWithWorks: z.boolean(), sort: z.enum(["featured", "count", "name"]),
  page: z.number().int().min(1), pageSize: z.union([z.literal(24), z.literal(48), z.literal(96)]),
  group: z.string().default(""),
  expanded: z.array(z.string()).nullable().default(null),
});
const defaults: z.infer<typeof browsingSchema> = { query: "", onlyWithWorks: false, sort: "featured", page: 1, pageSize: 24, expanded: null, group: "" };

type Props = {
  onOpenTag: (id: string, name?: string) => void;
  report?: (message: string) => void;
};

type CategoryBranch = { item: CategoryItem; key: string; children: CategoryBranch[] };

function categoryTree(categories: CategoryItem[], query: string, onlyWithWorks: boolean, sort: z.infer<typeof browsingSchema>["sort"]) {
  const nodes = categories.map(item => ({ id: item.id ?? item.slug, name: item.label, parent_id: item.parent_id, item }));
  if (sort !== "featured") nodes.sort((a, b) =>
    (sort === "count" ? b.item.work_count - a.item.work_count : 0) || a.name.localeCompare(b.name, "zh-CN"));
  const rows = tagTreeRows(nodes);
  const included = new Set<string>();
  let matches = 0;
  for (const { node, path } of rows) {
    if (onlyWithWorks && node.item.work_count === 0) continue;
    if (query && ![node.id, node.name, node.item.slug, ...node.item.aliases]
      .some(value => value.toLocaleLowerCase().includes(query))) continue;
    matches++;
    for (const ancestor of path) included.add(ancestor.id);
  }
  const branches = new Map<string, CategoryBranch>();
  const roots: CategoryBranch[] = [];
  for (const { node, path } of rows) {
    if (!included.has(node.id)) continue;
    const branch: CategoryBranch = { item: node.item, key: node.id, children: [] };
    const parent = path.at(-2);
    const siblings = parent ? branches.get(parent.id)!.children : roots;
    siblings.push(branch);
    branches.set(node.id, branch);
  }
  return { roots, matches, parents: [...branches.values()].filter(branch => branch.children.length) };
}

export function categoryWorksHref(tag: string) {
  return `?${new URLSearchParams({ tag })}`;
}

export function CategoriesView({ onOpenTag, report }: Props) {
  const [browsing, setBrowsing] = useSessionState("mdc:category-browsing", browsingSchema, defaults);
  const { query, onlyWithWorks, sort, pageSize } = browsing;
  const [composing, setComposing] = useState(false);
  const [queryDraft, setQueryDraft] = useState("");
  const heading = useRef<HTMLDivElement>(null);
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = document.querySelector(".top-nav");
    if (!nav || typeof ResizeObserver === "undefined") return;
    const update = () => section.current?.style.setProperty("--category-offset", `${nav.getBoundingClientRect().height + 8}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(nav);
    return () => observer.disconnect();
  }, []);
  const result = useQuery(resources.categories(), queryClient);
  const loading = result.isPending;
  useEffect(() => {
    if (result.error) report?.(result.error.message);
    else if (result.data) report?.(`分类 ${result.data.categories.length} 个`);
  }, [result.data, result.error, report]);

  const search = query.trim().toLocaleLowerCase();
  const { roots, matches, parents } = useMemo(() =>
    categoryTree(result.data?.categories ?? [], search, onlyWithWorks, sort), [result.data, search, onlyWithWorks, sort]);
  const expanded = new Set(browsing.expanded ?? roots.filter(branch => branch.children.length).map(branch => branch.key));
  function toggle(key: string) {
    const next = new Set(expanded);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setBrowsing(previous => ({ ...previous, expanded: [...next] }));
  }
  const page = Math.min(browsing.page, Math.max(1, Math.ceil(roots.length / pageSize)));
  useEffect(() => {
    if (result.data && page !== browsing.page) setBrowsing(previous => ({ ...previous, page }));
  }, [result.data, page, browsing.page, setBrowsing]);
  const group = !search && roots.find(branch => branch.key === browsing.group);
  const restored = useRef(false);
  useEffect(() => {
    const key = window.history.state?.categoryFocus;
    if (!result.data || restored.current || typeof key !== "string") return;
    const link = document.getElementById(`category-link-${key}`);
    if (!link) return;
    restored.current = true;
    link.focus({ preventScroll: true });
    link.scrollIntoView?.({ block: "center" });
  }, [result.data]);
  function selectGroup(key: string) {
    const next = new Set(expanded);
    if (key) next.add(key);
    setBrowsing(previous => ({ ...previous, group: key, page: 1, expanded: [...next] }));
    heading.current?.scrollIntoView?.({ block: "start" });
  }
  const items = group ? [group] : roots.slice((page - 1) * pageSize, page * pageSize);
  function changePage(next: number) {
    setBrowsing(previous => ({ ...previous, page: next }));
    heading.current?.scrollIntoView?.({ block: "start" });
  }
  const pagination = {
    label: "分类", page, pageSize, total: roots.length, busy: result.isFetching,
    onPageChange: changePage,
    onPageSizeChange: (size: number) => setBrowsing(previous => ({ ...previous, page: 1, pageSize: size as 24 | 48 | 96 })),
  };
  const filtered = Boolean(search || onlyWithWorks);
  function renderBranch({ item, key, children }: CategoryBranch) {
    const open = Boolean(search) || expanded.has(key);
    const tag = item.id ?? item.label;
    return <li key={key} className={open && children.length ? "category-open" : undefined}>
      <div className="category-tree-row">
        {children.length > 0 && !search
          ? <button type="button" className="category-disclosure" aria-expanded={open}
              aria-label={`${open ? "收起" : "展开"}${item.label}`} onClick={() => toggle(key)}><i aria-hidden="true" /></button>
          : <span className="category-guide" aria-hidden="true" />}
        <a className="category-works" id={`category-link-${key}`} href={categoryWorksHref(tag)}
          title={`查看「${item.label}」的作品`} onClick={event => {
            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            window.history.replaceState({ ...window.history.state, categoryFocus: key }, "");
            onOpenTag(tag, item.label);
          }}>
          {item.image_url && <CoverImage className="category-thumbnail" src={appUrl(item.image_url) ?? ""} />}
          <strong>{item.label}</strong>
          <span className="category-count">{item.work_count.toLocaleString()} 部<span className="category-word">作品</span></span>
          <span className="category-arrow" aria-hidden="true">→</span>
        </a>
      </div>
      {open && children.length > 0 && <ul>{children.map(renderBranch)}</ul>}
    </li>;
  }
  return <section className="panel-section categories-view" ref={section}>
    <div className="catalog-heading" ref={heading}>
      <div><p className="eyebrow">CATEGORIES</p><div className="category-heading-title"><h1>分类浏览</h1>
        <span className="muted">{loading ? "正在读取分类…" : `${filtered ? `匹配 ${matches} / ` : ""}${result.data?.categories.length ?? 0} 个分类`}</span>
      </div></div>
    </div>
    <div className="categories-toolbar">
      <input id="shadow-mdc-search" value={composing ? queryDraft : query}
        onCompositionStart={() => { setQueryDraft(query); setComposing(true); }}
        onCompositionEnd={event => {
          const value = event.currentTarget.value;
          setComposing(false);
          setBrowsing(previous => ({ ...previous, query: value, page: 1 }));
        }}
        onChange={event => {
          const nextValue = event.target.value;
          if (composing) setQueryDraft(nextValue);
          else setBrowsing(previous => ({ ...previous, query: nextValue, page: 1 }));
        }}
        placeholder="分类名称、别名…" aria-label="搜索分类" maxLength={200} />
      <select aria-label="分类排序" value={sort} onChange={event => setBrowsing(previous => ({
        ...previous, sort: event.target.value as typeof sort, page: 1,
      }))}>
        <option value="featured">推荐顺序</option><option value="count">作品最多</option><option value="name">分类名称</option>
      </select>
      <label className="categories-toggle"><input type="checkbox" checked={onlyWithWorks}
        onChange={event => setBrowsing(previous => ({ ...previous, onlyWithWorks: event.target.checked, page: 1 }))} />仅显示有作品</label>
      {!search && parents.length > 0 && <div className="category-tree-actions">
        <button type="button" className="ghost" disabled={parents.every(branch => expanded.has(branch.key))}
          onClick={() => setBrowsing(previous => ({ ...previous, expanded: parents.map(branch => branch.key) }))}>全部展开</button>
        <button type="button" className="ghost" disabled={!parents.some(branch => expanded.has(branch.key))}
          onClick={() => setBrowsing(previous => ({ ...previous, expanded: [] }))}>全部收起</button>
      </div>}
      {filtered && <button type="button" className="ghost" onClick={() => setBrowsing(previous => ({ ...previous, query: "", onlyWithWorks: false, page: 1 }))}>清除筛选</button>}
    </div>
    {result.error && <div className="catalog-error" role="alert">
      <span>分类加载失败：{result.error.message}</span>
      <button type="button" className="ghost" disabled={result.isFetching} onClick={() => void result.refetch()}>重试分类</button>
    </div>}
    {loading ? <p className="muted" role="status">正在读取分类树…</p> : result.data &&
      <div className={`category-layout${roots.length > 1 && !search ? " with-directory" : ""}`}>
        {roots.length > 1 && !search && <div className="category-directory">
          <div className="category-directory-heading"><strong>分类目录</strong><span>{roots.length}</span></div>
          <select aria-label="定位主分类" value={group ? group.key : ""} onChange={e => selectGroup(e.target.value)}>
            <option value="">全部分类</option>
            {roots.map(({ key, item }) => <option key={key} value={key}>{item.label}</option>)}
          </select>
          <nav aria-label="主分类目录">
            <button type="button" aria-pressed={!group} onClick={() => selectGroup("")}>全部分类</button>
            {roots.map(({ key, item }) => <button type="button" key={key} aria-pressed={Boolean(group && group.key === key)}
              onClick={() => selectGroup(key)}>{item.label}</button>)}
          </nav>
        </div>}
        <div className="category-results">
          {roots.length === 0 ? <div className="empty">
            <h2>{filtered ? "没有匹配的分类" : "暂无分类"}</h2>
            <p>{filtered ? "试试其他关键词，或清除筛选查看全部分类。" : "作品入库后，可在这里按分类浏览。"}</p>
          </div> : <ul className="category-tree" aria-label="分类树" aria-busy={result.isFetching}>
            {items.map(renderBranch)}
          </ul>}
          {!group && roots.length > defaults.pageSize && <Pagination {...pagination} />}
        </div>
    </div>}
  </section>;
}
