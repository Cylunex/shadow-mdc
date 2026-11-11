import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api";
import type { TagTerm } from "../lib/tagDictionary";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import { tagPathLabel, tagTreeRows } from "../lib/tagTree";

export function TagManagement({ onSaved }: { onSaved?: () => void } = {}) {
  const [open, setOpen] = useState(false);
  const [q, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<TagTerm | null>(null);
  const [saved, setSaved] = useState<TagTerm | null>(null);
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [hidden, setHidden] = useState(false);
  const [parentId, setParentId] = useState<string | null>(null);
  const [parentSearch, setParentSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const result = useQuery({ ...resources.tagDictionary(q, offset), enabled: open }, queryClient);
  const tree = useQuery({ ...resources.tagTree(), enabled: open }, queryClient);
  const rows = useMemo(() => tagTreeRows(tree.data?.nodes ?? []), [tree.data]);
  const paths = useMemo(() => new Map(rows.map(row => [row.node.id, tagPathLabel(row)])), [rows]);
  const parentRows = rows.filter(row => !row.path.some(node => node.id === selected?.id));
  const parentQuery = parentSearch.trim().toLocaleLowerCase();
  const parentOptions = parentRows.filter(row => !parentQuery || row.path.some(node =>
    [node.name, ...(node.aliases ?? [])].some(value => value.toLocaleLowerCase().includes(parentQuery)))).slice(0, 200);
  const currentParent = rows.find(row => row.node.id === parentId);
  const indexed = new Map((result.data?.terms ?? []).map(term => [term.id, term]));
  if (saved) indexed.set(saved.id, saved);
  if (selected && !indexed.has(selected.id)) indexed.set(selected.id, selected);
  const terms = [...indexed.values()];
  function edit(term: TagTerm | null) {
    setSelected(term); setName(term?.name ?? ""); setAliases(term?.aliases.join("、") ?? "");
    setHidden(term?.hidden ?? false); setParentId(term?.parent_id ?? null);
    setParentSearch(""); setMessage(""); setError("");
  }
  async function save() {
    if (!name.trim()) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const values = aliases.split(/[、,，\n]/).map(value => value.trim()).filter(Boolean);
      const term = selected
        ? await api.editTag(selected.id, { name: name.trim(), aliases: values, parent_id: parentId, hidden, revision: selected.revision })
        : await api.createTag({ name: name.trim(), aliases: values, parent_id: parentId, hidden });
      setSaved(term); edit(term); setMessage("标签已保存，已有作品和片单关联继续保留。");
      onSaved?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "标签保存失败"); }
    finally { setBusy(false); }
  }
  return <details className="catalog-management" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>标签字典</summary>
    {open && <div className="catalog-tools">
      <label>查找标签<input aria-label="查找标签词条" value={q} maxLength={300}
        onChange={event => { setQuery(event.target.value); setOffset(0); }} /></label>
      {result.isPending ? <p role="status">正在读取标签字典…</p> : result.error ? <p role="alert">标签字典读取失败
        <button type="button" onClick={() => void result.refetch()}>重试</button></p>
        : <>
          <form onSubmit={event => { event.preventDefault(); void save(); }} className="catalog-tools">
            <label>标签词条<select aria-label="管理标签词条" value={selected?.id ?? ""} disabled={busy}
              onChange={event => edit(terms.find(term => term.id === event.target.value) ?? null)}>
              <option value="">新增标签词条</option>{terms.map(term => <option key={term.id} value={term.id}>
                {paths.get(term.id) || term.path.map(node => node.name).join(" › ") || term.name}{term.hidden ? "（已隐藏）" : ""} · {term.count}</option>)}</select></label>
            <label>展示名称<input aria-label="标签展示名称" value={name} maxLength={300} required disabled={busy}
              onChange={event => setName(event.target.value)} /></label>
            <label>搜索别名<textarea aria-label="标签搜索别名" value={aliases} disabled={busy}
              onChange={event => setAliases(event.target.value)} placeholder="多个别名用顿号分隔" /></label>
            <label>查找上级<input aria-label="搜索上级标签" value={parentSearch} disabled={busy}
              onChange={event => setParentSearch(event.target.value)} placeholder="按标签路径查找" /></label>
            <label>上级节点<select aria-label="标签上级节点" value={parentId ?? ""} disabled={busy || tree.isPending || Boolean(tree.error)}
              onChange={event => setParentId(event.target.value || null)}>
              <option value="">顶层节点</option>{parentId && !parentOptions.some(row => row.node.id === parentId)
                && <option value={parentId}>{currentParent ? tagPathLabel(currentParent) : "原上级节点"}</option>}
              {parentOptions.map(row => <option key={row.node.id} value={row.node.id}>{tagPathLabel(row)}</option>)}
            </select></label>
            {tree.error && <p role="alert">上级节点读取失败<button type="button" onClick={() => void tree.refetch()}>重试</button></p>}
            <p className="muted">分组也是高层标签节点。筛选节点包含其下级，作品只需标记具体标签。</p>
            <label><input type="checkbox" aria-label="隐藏标签" checked={hidden} disabled={busy}
              onChange={event => setHidden(event.target.checked)} />在分类和筛选中隐藏</label>
            <button type="submit" disabled={busy || !name.trim()}>{busy ? "正在保存…" : "保存标签词条"}</button>
            {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
          </form>
          <div><button type="button" disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>上一页标签</button>
            <span>共 {result.data?.total ?? 0} 项</span><button type="button"
              disabled={offset + 100 >= (result.data?.total ?? 0)} onClick={() => setOffset(value => value + 100)}>下一页标签</button></div>
        </>}
    </div>}
  </details>;
}
