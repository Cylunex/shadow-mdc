import { useState } from "react";
import { api } from "../api";
import type { ClassificationTerm } from "../lib/classifications";

export function ClassificationTermEditor({ dimension, terms }: { dimension: string; terms: ClassificationTerm[] }) {
  const [saved, setSaved] = useState<ClassificationTerm | null>(null);
  const indexed = new Map(terms.map(term => [term.id, term]));
  if (saved) indexed.set(saved.id, saved);
  const choices = [...indexed.values()];
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [revision, setRevision] = useState(1);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function save() {
    if (!name.trim()) return;
    setBusy(true); setMessage("");
    try {
      const values = aliases.split(/[、,，]/).map(value => value.trim()).filter(Boolean);
      const term = selected ? await api.editClassification(selected, name, values, revision)
        : await api.createClassification(dimension, name, values);
      setSaved(term); setSelected(term.id); setName(term.name); setAliases(term.aliases.join("、")); setRevision(term.revision);
      setMessage("分类已保存，已有作品关联继续保留。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <details className="catalog-management"><summary>管理本维度的分类项</summary>
    <form className="catalog-tools" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>分类项<select aria-label="管理分类项" value={selected} disabled={busy} onChange={event => {
        const term = choices.find(value => value.id === event.target.value);
        setSelected(term?.id ?? ""); setName(term?.name ?? ""); setAliases(term?.aliases.join("、") ?? ""); setRevision(term?.revision ?? 1); setMessage("");
      }}><option value="">新增分类项</option>{choices.map(term => <option key={term.id} value={term.id}>{term.name}</option>)}</select></label>
      <label>展示名称<input aria-label="分类展示名称" maxLength={300} required value={name} disabled={busy} onChange={event => setName(event.target.value)} /></label>
      <label>搜索别名<input aria-label="分类搜索别名" value={aliases} disabled={busy} onChange={event => setAliases(event.target.value)} placeholder="多个别名用顿号分隔" /></label>
      <button type="submit" disabled={busy || !name.trim()}>{busy ? "正在保存…" : "保存分类项"}</button>
      {message && <p role="status">{message}</p>}
    </form>
  </details>;
}
