import { useState } from "react";

type Tag = { id: string; name: string; count: number; aliases?: string[]; group?: string };
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
  const [group, setGroup] = useState("");
  const groups = [...new Set(tags.map(tag => tag.group).filter((value): value is string => Boolean(value)))].sort();
  const activeGroup = groups.includes(group) ? group : "";
  const query = fold(search);
  const matches = tags.filter(tag => (!activeGroup || tag.group === activeGroup) && (!query || [tag.id, tag.name, ...(tag.aliases ?? [])]
    .some(value => fold(value).includes(query))));
  const isSelected = (tag: Tag) => selected.some(value => matchesTag(tag, value));
  return <>
    {preview && <div className="catalog-tag-preview" aria-label="常用分类标签">
      {[...tags].sort((a, b) => b.count - a.count).slice(0, 8).map(tag => <button key={tag.id}
        className={isSelected(tag) ? "active" : "ghost"}
        aria-pressed={isSelected(tag)} onClick={() => onToggle(tag.id)}>{tag.name} · {tag.count}</button>)}
    </div>}
    <details className="catalog-tags">
    <summary>标签 · {tags.length}</summary>
    {groups.length > 0 && <select aria-label="浏览标签分组" value={activeGroup}
      onChange={event => { setGroup(event.target.value); setLimit(PAGE_SIZE); }}>
      <option value="">全部标签分组</option>{groups.map(value => <option key={value} value={value}>{value}</option>)}
    </select>}
    <input aria-label="搜索分类标签" placeholder="搜索全部标签" value={search}
      onChange={event => { setSearch(event.target.value); setLimit(PAGE_SIZE); }} />
    <div className="tag-capsules" role="group" aria-label="分类标签">
      {matches.slice(0, limit).map(tag => <button key={tag.id}
        className={isSelected(tag) ? "active" : "ghost"}
        aria-pressed={isSelected(tag)} onClick={() => onToggle(tag.id)}>{tag.name} · {tag.count}</button>)}
      {matches.length === 0 && <span>没有匹配的标签</span>}
      {matches.length > limit && <button className="ghost" onClick={() => setLimit(value => value + PAGE_SIZE)}>更多标签（{limit} / {matches.length}）</button>}
    </div>
  </details></>;
}
