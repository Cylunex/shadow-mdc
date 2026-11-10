import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { CLASSIFICATION_KEYS, CLASSIFICATION_NAMES } from "../lib/classifications";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import { TaxonomyEditor } from "./TaxonomyEditor";

export function ClassificationDetails({ work }: { work: { id: string; title: string } }) {
  const result = useQuery(resources.taxonomy(work.id), queryClient);
  const [editing, setEditing] = useState(false);
  return <section className="work-classifications"><h3>作品分类 <button className="ghost" onClick={() => setEditing(true)}>编辑分类</button></h3>
    {result.error ? <p role="alert">分类读取失败 <button onClick={() => void result.refetch()}>重试</button></p>
      : result.isPending ? <p role="status">正在读取分类…</p> : <dl>{CLASSIFICATION_KEYS.map(key => <div key={key}>
        <dt>{CLASSIFICATION_NAMES[key]}</dt><dd>{result.data.classification_refs[key]?.map(term => term.name).join("、") || "待补"}</dd>
      </div>)}</dl>}
    {editing && <TaxonomyEditor work={work} onClose={() => setEditing(false)} onSaved={() => setEditing(false)} />}
  </section>;
}
