import { useEffect, useState } from "react";
import { z } from "zod";
import { catalogRequest } from "./CatalogManagement";
import { metadataGapNames, metadataGapSchema, type Filters } from "../lib/catalogFilters";

const metadataGapCountsSchema = z.array(z.object({ id: metadataGapSchema, count: z.number().int().nonnegative() }));

export default function MetadataGapFilter({ filters, revision, refresh, onSelect }: {
  filters: Filters; revision: number; refresh: number; onSelect: (gap: Filters["missing_metadata"]) => void;
}) {
  const [gapCounts, setGapCounts] = useState<z.infer<typeof metadataGapCountsSchema>>([]);
  const [gapError, setGapError] = useState(false);
  const [gapRetry, setGapRetry] = useState(0);
  const gapScope = JSON.stringify({ ...filters, missing_metadata: undefined });
  useEffect(() => {
    const controller = new AbortController();
    setGapCounts([]);
    setGapError(false);
    const timer = setTimeout(() => {
      catalogRequest<unknown>("metadata-gaps", "POST", JSON.parse(gapScope), controller.signal)
        .then(value => {
          if (!controller.signal.aborted) setGapCounts(metadataGapCountsSchema.parse(value));
        })
        .catch(() => { if (!controller.signal.aborted) setGapError(true); });
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [gapScope, revision, refresh, gapRetry]);
  return <>
      <label>资料待补<select aria-label="资料待补" value={filters.missing_metadata ?? ""}
        onChange={event => onSelect(event.target.value ? metadataGapSchema.parse(event.target.value) : undefined)}>
        <option value="">全部作品</option>
        {metadataGapSchema.options.map(id => {
          const count = gapCounts.find(gap => gap.id === id)?.count;
          return <option key={id} value={id}>{metadataGapNames[id]}{count === undefined ? "" : ` · ${count}`}</option>;
        })}
      </select></label>
      {gapError && <button onClick={() => setGapRetry(value => value + 1)}>重试资料缺项统计</button>}
  </>;
}
