import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { CLASSIFICATION_KEYS, CLASSIFICATION_NAMES, type ClassificationKey } from "../lib/classifications";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import { ClassificationTermEditor } from "./ClassificationTermEditor";

export function ClassificationManagement() {
  const [open, setOpen] = useState(false);
  const [dimension, setDimension] = useState<ClassificationKey>("contents");
  const result = useQuery({ ...resources.classifications(), enabled: open }, queryClient);
  return <details className="catalog-management" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>分类字典</summary>
    {open && <div className="catalog-tools">
      <label>分类维度<select aria-label="分类字典维度" value={dimension}
        onChange={event => setDimension(event.target.value as ClassificationKey)}>
        {CLASSIFICATION_KEYS.map(key => <option key={key} value={key}>{CLASSIFICATION_NAMES[key]}</option>)}
      </select></label>
      {result.isPending ? <p role="status">正在读取分类字典…</p> : result.error ? <p role="alert">分类字典读取失败
        <button type="button" onClick={() => void result.refetch()}>重试</button></p>
        : <ClassificationTermEditor key={dimension} dimension={dimension}
          terms={result.data.dimensions.find(item => item.id === dimension)?.values ?? []} />}
    </div>}
  </details>;
}
