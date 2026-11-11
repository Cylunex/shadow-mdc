import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import { CLASSIFICATION_KEYS, CLASSIFICATION_NAMES, taxonomyEditSchema, type TaxonomyEdit } from "../lib/classifications";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

export function TaxonomyEditor({ work, onClose, onSaved }: {
  work: { id: string; title: string }; onClose: () => void; onSaved: () => void;
}) {
  const schema = useQuery(resources.classifications(), queryClient);
  const document = useQuery(resources.taxonomy(work.id), queryClient);
  const [value, setValue] = useState<TaxonomyEdit>(taxonomyEditSchema.parse({}));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { if (document.data) setValue(taxonomyEditSchema.parse(document.data)); }, [document.data]);
  async function save() {
    setSaving(true); setError("");
    try { await api.saveWorkTaxonomy(work.id, value); onSaved(); }
    catch (error) { setError(error instanceof Error ? error.message : "分类保存失败"); }
    finally { setSaving(false); }
  }
  return <div className="genre-sheet-backdrop"><form className="genre-sheet taxonomy-editor" role="dialog" aria-modal="true" aria-label="编辑作品分类"
    onSubmit={event => { event.preventDefault(); void save(); }}>
    <h2>{work.title}</h2><p>同一维度可多选；题材统一在作品标签中维护。</p>
    {document.error || schema.error ? <p role="alert">分类读取失败 <button type="button" onClick={() => { void document.refetch(); void schema.refetch(); }}>重试</button></p>
    : document.isPending || schema.isPending ? <p role="status">正在读取分类…</p> : <>
      <div className="taxonomy-fields">{CLASSIFICATION_KEYS.map(key => <label key={key}>{CLASSIFICATION_NAMES[key]}
        <select multiple size={4} aria-label={`编辑${CLASSIFICATION_NAMES[key]}`} value={value[key]} disabled={saving}
          onChange={event => setValue(previous => ({ ...previous, [key]: [...event.target.selectedOptions].map(option => option.value) }))}>
          {schema.data?.dimensions.find(dimension => dimension.id === key)?.values.map(term => <option key={term.id} value={term.id}>{term.name}</option>)}
        </select>
      </label>)}</div>
      <label>发行年<input aria-label="编辑发行年" type="number" min={1800} max={2200} value={value.release_year ?? ""} disabled={saving}
        onChange={event => setValue(previous => ({ ...previous, release_year: event.target.value ? Number(event.target.value) : null }))} /></label>
      <p className="muted">语言表示作品原始对白语言；配音和字幕属于具体片源。未掌握的资料可留空。</p>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={saving}>{saving ? "正在保存…" : "保存"}</button>
    </>}
    <button type="button" disabled={saving} onClick={onClose}>取消</button>
  </form></div>;
}
