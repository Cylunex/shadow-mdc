import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import type { TagTerm } from "../lib/tagDictionary";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

export function TagManagement() {
  const [open, setOpen] = useState(false);
  const [q, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<TagTerm | null>(null);
  const [saved, setSaved] = useState<TagTerm | null>(null);
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [hidden, setHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const result = useQuery({ ...resources.tagDictionary(q, offset), enabled: open }, queryClient);
  const indexed = new Map((result.data?.terms ?? []).map(term => [term.id, term]));
  if (saved) indexed.set(saved.id, saved);
  if (selected && !indexed.has(selected.id)) indexed.set(selected.id, selected);
  const terms = [...indexed.values()];
  function edit(term: TagTerm | null) {
    setSelected(term); setName(term?.name ?? ""); setAliases(term?.aliases.join("、") ?? "");
    setHidden(term?.hidden ?? false); setMessage(""); setError("");
  }
  async function save() {
    if (!name.trim()) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const values = aliases.split(/[、,，\n]/).map(value => value.trim()).filter(Boolean);
      const term = selected
        ? await api.editTag(selected.id, { name: name.trim(), aliases: values, hidden, revision: selected.revision })
        : await api.createTag({ name: name.trim(), aliases: values, hidden });
      setSaved(term); edit(term); setMessage("标签已保存，已有作品和片单关联继续保留。");
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
                {term.name}{term.hidden ? "（已隐藏）" : ""} · {term.count}</option>)}</select></label>
            <label>展示名称<input aria-label="标签展示名称" value={name} maxLength={300} required disabled={busy}
              onChange={event => setName(event.target.value)} /></label>
            <label>搜索别名<textarea aria-label="标签搜索别名" value={aliases} disabled={busy}
              onChange={event => setAliases(event.target.value)} placeholder="多个别名用顿号分隔" /></label>
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
