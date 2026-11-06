import { useState } from "react";

type Tag = { id: string; name: string; count: number };
const PAGE_SIZE = 48;

export function CatalogTags({ tags, selected, preview, onToggle }: {
  tags: Tag[]; selected: string[]; preview: boolean; onToggle: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const query = search.trim().toLocaleLowerCase();
  const matches = tags.filter(tag => !query || tag.name.toLocaleLowerCase().includes(query));
  return <>
    {preview && <div className="catalog-tag-preview" aria-label="常用分类标签">
      {[...tags].sort((a, b) => b.count - a.count).slice(0, 8).map(tag => <button key={tag.id}
        className={selected.includes(tag.id) ? "active" : "ghost"}
        aria-pressed={selected.includes(tag.id)} onClick={() => onToggle(tag.id)}>{tag.name} · {tag.count}</button>)}
    </div>}
    <details className="catalog-tags">
    <summary>标签 · {tags.length}</summary>
    <input aria-label="搜索分类标签" placeholder="搜索全部标签" value={search}
      onChange={event => { setSearch(event.target.value); setLimit(PAGE_SIZE); }} />
    <div className="tag-capsules" role="group" aria-label="分类标签">
      {matches.slice(0, limit).map(tag => <button key={tag.id}
        className={selected.includes(tag.id) ? "active" : "ghost"}
        aria-pressed={selected.includes(tag.id)} onClick={() => onToggle(tag.id)}>{tag.name} · {tag.count}</button>)}
      {matches.length === 0 && <span>没有匹配的标签</span>}
      {matches.length > limit && <button className="ghost" onClick={() => setLimit(value => value + PAGE_SIZE)}>更多标签（{limit} / {matches.length}）</button>}
    </div>
  </details></>;
}
