import { useEffect, useState } from "react";
import { api } from "../api";

type Props = {
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
  report: (message: string) => void;
};

export function FieldPrioritySettings({ busy, run, report }: Props) {
  const [priorities, setPriorities] = useState<Record<string, string[]>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    void api.fieldPriority().then((payload) => {
      setPriorities(payload.priorities);
      const next: Record<string, string> = {};
      for (const [field, sources] of Object.entries(payload.priorities)) {
        next[field] = sources.join(", ");
      }
      setDraft(next);
    }).catch((error) => report(error instanceof Error ? error.message : String(error)));
  }, [report]);

  return (
    <section className="field-priority-settings">
      <h2>字段来源优先级</h2>
      <p>每行一个字段，逗号分隔来源名；越靠前优先级越高。锁定仍以作品级 field_locks 为准。</p>
      {Object.keys(draft).map((field) => (
        <label key={field} className="field-priority-row">
          <span>{field}</span>
          <input
            value={draft[field] ?? ""}
            onChange={(e) => setDraft((current) => ({ ...current, [field]: e.target.value }))}
          />
        </label>
      ))}
      <button
        type="button"
        disabled={busy === "field-priority"}
        onClick={() => {
          void run("field-priority", async () => {
            const next: Record<string, string[]> = {};
            for (const [field, value] of Object.entries(draft)) {
              next[field] = value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            }
            const saved = await api.saveFieldPriority(next);
            setPriorities(saved.priorities);
            report("字段优先级已保存");
          });
        }}
      >
        保存优先级
      </button>
      <details>
        <summary>当前 JSON</summary>
        <pre>{JSON.stringify(priorities, null, 2)}</pre>
      </details>
    </section>
  );
}
