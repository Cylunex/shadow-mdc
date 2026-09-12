import { FormEvent, useState } from "react";
import { api } from "../api";

type Props = {
  busy: string | null;
  run: (key: string, action: () => Promise<void>, refresh?: "none" | "works" | "actors" | "inbox" | "tasks" | "core" | "all") => Promise<void>;
  report: (message: string) => void;
};

export function IdentifyByUrlPanel({ busy, run, report }: Props) {
  const [code, setCode] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [externalId, setExternalId] = useState("");
  const [provider, setProvider] = useState("theporndb");

  function submit(event: FormEvent) {
    event.preventDefault();
    void run("identify-url", async () => {
      const external_ids = externalId.trim() ? { [provider]: externalId.trim() } : {};
      const result = await api.lookupWork({
        code: code.trim() || undefined,
        source_url: sourceUrl.trim() || undefined,
        external_ids
      });
      report(
        result.work
          ? `已识别作品：${result.work.title}`
          : `未命中正式作品（匹配记录 ${result.matched_records}）`
      );
    }, "works");
  }

  return (
    <section className="identify-url-panel">
      <h2>URL / 外部 ID 识别</h2>
      <p>用详情页 URL、番号或 provider ID 直接建档，不必先扫进媒体库。</p>
      <form onSubmit={submit} className="identify-url-form">
        <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="番号（可选）" />
        <input
          value={sourceUrl}
          onChange={(e) => setSourceUrl(e.target.value)}
          placeholder="详情页 URL（可选）"
        />
        <div className="row">
          <select value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="provider">
            <option value="theporndb">ThePornDB</option>
            <option value="javdb">JavDB</option>
            <option value="r18dev">R18.dev</option>
            <option value="jsonld">JSON-LD</option>
          </select>
          <input
            value={externalId}
            onChange={(e) => setExternalId(e.target.value)}
            placeholder="外部 ID（可选）"
          />
        </div>
        <button disabled={busy === "identify-url" || (!code.trim() && !sourceUrl.trim() && !externalId.trim())}>
          识别
        </button>
      </form>
    </section>
  );
}
