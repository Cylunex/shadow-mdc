import { useEffect, useState } from "react";

type Props = {
  label: string;
  position?: "顶部" | "底部";
  page: number;
  pageSize: number;
  total: number;
  busy?: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
};

export function Pagination({ label, position = "顶部", page, pageSize, total, busy = false,
  onPageChange, onPageSizeChange }: Props) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const [target, setTarget] = useState(String(page));
  useEffect(() => setTarget(String(page)), [page]);
  const numbers = [...new Set([1, page - 1, page, page + 1, pages])]
    .filter(value => value >= 1 && value <= pages).sort((a, b) => a - b);
  return <nav className="catalog-pagination" aria-label={`${label}分页（${position}）`} aria-busy={busy}>
    <span className="catalog-page-count" role="status">
      {total === 0 ? "共 0 项" : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)} / ${total} 项`}
      {` · 第 ${page} / ${pages} 页`}
    </span>
    <div className="catalog-page-buttons">
      <button type="button" className="ghost" aria-label={`上一页${label}`} disabled={busy || page <= 1}
        onClick={() => onPageChange(page - 1)}>上一页</button>
      {numbers.map((number, index) => <span className="catalog-page-number" key={number}>
        {index > 0 && number > numbers[index - 1]! + 1 && <span aria-hidden="true">…</span>}
        <button type="button" className="ghost" aria-label={`第 ${number} 页${label}`}
          aria-current={number === page ? "page" : undefined} disabled={busy || number === page}
          onClick={() => onPageChange(number)}>{number}</button>
      </span>)}
      <button type="button" className="ghost" aria-label={`下一页${label}`} disabled={busy || page >= pages}
        onClick={() => onPageChange(page + 1)}>下一页</button>
    </div>
    {position === "顶部" && <label className="catalog-page-size">每页
      <select aria-label={`${label}每页数量`} value={pageSize} disabled={busy}
        onChange={event => onPageSizeChange(Number(event.target.value))}>
        {[24, 48, 96].map(size => <option key={size} value={size}>{size} 项</option>)}
      </select>
    </label>}
    {pages > 7 && <form className="catalog-page-jump" onSubmit={event => {
      event.preventDefault();
      const next = Number(target);
      if (Number.isInteger(next) && next >= 1 && next <= pages && next !== page) onPageChange(next);
    }}>
      <input type="number" inputMode="numeric" aria-label={`${label}跳转页码（${position}）`}
        min={1} max={pages} required value={target} disabled={busy}
        onChange={event => setTarget(event.target.value)} />
      <button className="ghost" disabled={busy}>跳转</button>
    </form>}
  </nav>;
}
