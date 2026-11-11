import { useMemo, useState } from "react";
import { tagTreeRows, type TreeNode } from "../lib/tagTree";

type Tag = TreeNode & { count: number; group?: string };
const PAGE_SIZE = 48;
const fold = (text: string) => text.normalize("NFKC").trim().toLowerCase();

export function matchesTag(tag: Tag, value: string): boolean {
  return [tag.id, tag.name, ...(tag.aliases ?? [])].some(alias => fold(alias) === fold(value));
}

export function CatalogTags({ tags, selected, preview, onToggle }: {
  tags: Tag[]; selected: string[]; preview: boolean; onToggle: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const rows = useMemo(() => tagTreeRows(tags), [tags]);
  const query = fold(search);
  const isSelected = (tag: Tag) => selected.some(value => matchesTag(tag, value));
  const selectedAncestors = new Set(rows.filter(row => isSelected(row.node))
    .flatMap(row => row.path.slice(0, -1).map(node => node.id)));
  const isExpanded = (id: string) => expanded[id] ?? selectedAncestors.has(id);
  const matching = new Set<string>();
  if (query) for (const row of rows) {
    if (row.path.some(node => [node.id, node.name, ...(node.aliases ?? [])].some(value => fold(value).includes(query)))) {
      for (const node of row.path) matching.add(node.id);
    }
  }
  const visible = rows.filter(row => query ? matching.has(row.node.id)
    : row.path.slice(0, -1).every(node => isExpanded(node.id)));
  return <>
    {preview && <div className="catalog-tag-preview" aria-label="常用分类标签">
      {[...tags].sort((a, b) => b.count - a.count).slice(0, 8).map(tag => <button key={tag.id}
        className={isSelected(tag) ? "active" : "ghost"}
        aria-pressed={isSelected(tag)} onClick={() => onToggle(tag.id)}>{tag.name} · {tag.count}</button>)}
    </div>}
    <details className="catalog-tags">
    <summary>标签树 · {tags.length}</summary>
    <input aria-label="搜索分类标签" placeholder="搜索标签及其上级" value={search}
      onChange={event => { setSearch(event.target.value); setLimit(PAGE_SIZE); }} />
    <ul className="catalog-tag-tree" aria-label="标签树">
      {visible.slice(0, limit).map(({ node, depth, hasChildren, path }) => <li key={node.id}
        style={{ paddingInlineStart: Math.min(depth, 8) * 12 }} title={path.map(part => part.name).join(" › ")}>
        {hasChildren && query && <span className="catalog-tag-expand" aria-hidden="true">▾</span>}
        {hasChildren && !query && <button className="ghost catalog-tag-expand" aria-label={`${isExpanded(node.id) ? "收起" : "展开"}${node.name}`}
          aria-expanded={isExpanded(node.id)} onClick={() => {
            setExpanded(previous => ({ ...previous, [node.id]: !(previous[node.id] ?? selectedAncestors.has(node.id)) }));
            setLimit(PAGE_SIZE);
          }}>{isExpanded(node.id) ? "▾" : "▸"}</button>}
        {!hasChildren && <span className="catalog-tag-expand" aria-hidden="true" />}
        <button className={isSelected(node) ? "active" : "ghost"} aria-pressed={isSelected(node)}
          onClick={() => onToggle(node.id)}>{node.name} · {node.count}</button>
      </li>)}
    </ul>
    {visible.length === 0 && <span>没有匹配的标签</span>}
    {visible.length > limit && <button className="ghost" onClick={() => setLimit(value => value + PAGE_SIZE)}>
      更多标签（{limit} / {visible.length}）</button>}
  </details></>;
}
