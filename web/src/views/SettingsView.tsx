import { useEffect, useState } from "react";
import { api } from "../api";
import { FieldPrioritySettings } from "../components/FieldPrioritySettings";
import { IdentifyByUrlPanel } from "../components/IdentifyByUrlPanel";
import type { Library, MediaServerSettings } from "../model";
import { AliasEditor, CatalogImportEditor, FilterWordsEditor, Libraries, ProviderDiagnostics } from "../panels";

type Props = {
  libraries: Library[];
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
  report: (message: string) => void;
};

export function SettingsView({ libraries, busy, run, report }: Props) {
  const [pan, setPan] = useState<{ provider: string; available: boolean; reason: string } | null>(null);
  const [media, setMedia] = useState<MediaServerSettings | null>(null);

  useEffect(() => {
    void api.panStatus().then(setPan).catch(() => setPan(null));
    void api.mediaServer().then(setMedia).catch(() => setMedia(null));
  }, []);

  return (
    <section className="panel-section settings-view">
      <div className="section-hero">
        <div>
          <p className="eyebrow">SYSTEM</p>
          <h1>设置</h1>
          <p className="muted">媒体库、来源、Emby 深链与 115 占位；不改变现有 API 契约。</p>
        </div>
      </div>

      <div className="settings-grid">
        <article className="settings-card">
          <h2>115 网盘（占位）</h2>
          <p className="muted">
            {pan
              ? `${pan.provider} · ${pan.available ? "可用" : "不可用"} — ${pan.reason}`
              : "正在读取 /api/pan/status…"}
          </p>
          <p className="roadmap-note">
            路线图：Open Platform 开发者审批通过后，再接入 OAuth 与离线任务；当前仅展示状态 stub，不提供真实鉴权或下载。
          </p>
        </article>

        <article className="settings-card">
          <h2>Emby / Jellyfin 深链</h2>
          {media && (
            <form
              className="settings-form"
              onSubmit={(event) => {
                event.preventDefault();
                void run("media-server", async () => {
                  const saved = await api.saveMediaServer(media);
                  setMedia(saved);
                  report("媒体服务器设置已保存");
                });
              }}
            >
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={media.enabled}
                  onChange={(e) => setMedia({ ...media, enabled: e.target.checked })}
                />
                启用整理后刷新
              </label>
              <label>
                <span>类型</span>
                <select value={media.kind} onChange={(e) => setMedia({ ...media, kind: e.target.value })}>
                  <option value="emby">Emby</option>
                  <option value="jellyfin">Jellyfin</option>
                </select>
              </label>
              <label>
                <span>Base URL</span>
                <input
                  value={media.base_url ?? ""}
                  onChange={(e) => setMedia({ ...media, base_url: e.target.value || null })}
                  placeholder="https://emby.example:8096"
                />
              </label>
              <label>
                <span>API Key</span>
                <input
                  value={media.api_key ?? ""}
                  onChange={(e) => setMedia({ ...media, api_key: e.target.value || null })}
                  placeholder="可选"
                />
              </label>
              <label>
                <span>深链模板（可选，支持 {"{query}"}）</span>
                <input
                  value={media.deep_link_template ?? ""}
                  onChange={(e) => setMedia({ ...media, deep_link_template: e.target.value || null })}
                  placeholder="留空则按 Base URL 自动生成搜索链"
                />
              </label>
              <button type="submit" disabled={busy === "media-server"}>保存媒体服务器</button>
            </form>
          )}
        </article>
      </div>

      <Libraries libraries={libraries} busy={busy} run={run} report={report} />
      <IdentifyByUrlPanel busy={busy} run={run} report={report} />
      <FieldPrioritySettings busy={busy} run={run} report={report} />
      <ProviderDiagnostics />
      <FilterWordsEditor />
      <AliasEditor />
      <CatalogImportEditor busy={busy} run={run} report={report} />
    </section>
  );
}
