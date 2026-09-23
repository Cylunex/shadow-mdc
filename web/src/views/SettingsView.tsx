import { useEffect, useState } from "react";
import { api } from "../api";
import { FieldPrioritySettings } from "../components/FieldPrioritySettings";
import { IdentifyByUrlPanel } from "../components/IdentifyByUrlPanel";
import type { Library, MediaServerSettings, PanOfflineTask, PanSettings, PanStatus } from "../model";
import { AliasEditor, CatalogImportEditor, FilterWordsEditor, Libraries, ProviderDiagnostics } from "../panels";

type RefreshMode = "none" | "works" | "actors" | "inbox" | "tasks" | "core" | "all";

type Props = {
  libraries: Library[];
  busy: string | null;
  run: (key: string, action: () => Promise<void>, refresh?: RefreshMode) => Promise<void>;
  report: (message: string) => void;
};

export function SettingsView({ libraries, busy, run, report }: Props) {
  const [pan, setPan] = useState<PanStatus | null>(null);
  const [panSettings, setPanSettings] = useState<PanSettings | null>(null);
  const [offlineTasks, setOfflineTasks] = useState<PanOfflineTask[]>([]);
  const [media, setMedia] = useState<MediaServerSettings | null>(null);
  const [qr, setQr] = useState<{ id: string; qr_code: string } | null>(null);
  const [loginState, setLoginState] = useState<string | null>(null);
  const [dirDraft, setDirDraft] = useState("");
  const [clientIdDraft, setClientIdDraft] = useState("");
  const [clientSecretDraft, setClientSecretDraft] = useState("");
  const [accessTokenDraft, setAccessTokenDraft] = useState("");
  const [refreshTokenDraft, setRefreshTokenDraft] = useState("");
  const [browseId, setBrowseId] = useState("0");
  const [browseItems, setBrowseItems] = useState<Array<{ id: string; name: string; is_directory: boolean }>>([]);

  const reloadPan = async () => {
    const [status, settings, tasks] = await Promise.all([
      api.panStatus(),
      api.panSettings().catch(() => null),
      api.panOfflineTasks().catch(() => []),
    ]);
    setPan(status);
    if (settings) {
      setPanSettings(settings);
      setDirDraft(settings.offline_directory_id ?? "");
      setClientIdDraft(settings.client_id);
      setClientSecretDraft("");
    }
    setOfflineTasks(tasks);
  };

  useEffect(() => {
    void reloadPan().catch(() => setPan(null));
    void api.mediaServer().then(setMedia).catch(() => setMedia(null));
  }, []);

  useEffect(() => {
    if (!qr) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const status = await api.panLoginStatus(qr.id);
          if (cancelled) return;
          setLoginState(status.state);
          if (status.state === "ok") {
            window.clearInterval(timer);
            setQr(null);
            report("115 登录成功");
            await reloadPan();
          } else if (status.state === "expired" || status.state === "canceled" || status.state === "error") {
            window.clearInterval(timer);
            report(`115 登录结束：${status.state}${status.error ? ` — ${status.error}` : ""}`);
          }
        } catch {
          /* ignore transient poll errors */
        }
      })();
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [qr, report]);

  return (
    <section className="panel-section settings-view">
      <div className="section-hero">
        <div>
          <p className="eyebrow">SYSTEM</p>
          <h1>设置</h1>
          <p className="muted">媒体库、来源、Emby 深链与 115 云下载 / STRM。</p>
        </div>
      </div>

      <div className="settings-grid">
        <article className="settings-card">
          <h2>115 网盘</h2>
          <p className="muted">
            {pan
              ? `${pan.provider} · ${pan.available ? "可用" : "不可用"} — ${pan.reason}`
              : "正在读取 /api/pan/status…"}
          </p>
          {pan?.account && (
            <p className="muted">
              账号 {pan.account.user_name || pan.account.user_id || "已连接"}
              {pan.account.expires_at ? ` · token 至 ${pan.account.expires_at}` : ""}
            </p>
          )}
          <p className="roadmap-note">
            使用 Open Platform 设备码 + PKCE 登录。默认 client_id={pan?.client_id || "100197303"}（社区临时应用，建议在 open.115.com 申请自有应用并通过 SHADOW_MDC_PAN_CLIENT_ID 配置）。
            磁力仍保存在本地；「推到 115 离线」是单独动作。建议 115 请求直连（勿经代理），以免触发风控。
          </p>
          <div className="magnet-row" style={{ gridTemplateColumns: "1fr auto auto" }}>
            <span>{pan?.connected ? "已登录" : "未登录"}</span>
            <button
              type="button"
              disabled={busy === "pan-login"}
              onClick={() => {
                void run("pan-login", async () => {
                  const started = await api.panLogin();
                  setQr(started);
                  setLoginState("waiting");
                  report("请使用 115 App 扫描二维码");
                }, "none");
              }}
            >二维码登录</button>
            <button
              type="button"
              className="secondary"
              disabled={!pan?.connected || busy === "pan-disconnect"}
              onClick={() => {
                void run("pan-disconnect", async () => {
                  await api.panDisconnect();
                  setQr(null);
                  await reloadPan();
                  report("已断开 115");
                }, "none");
              }}
            >断开</button>
          </div>
          {qr && (
            <div style={{ marginTop: 12, textAlign: "center" }}>
              <img
                alt="115 login QR"
                src={`data:image/png;base64,${qr.qr_code}`}
                style={{ width: 220, height: 220, background: "#fff", borderRadius: 8 }}
              />
              <p className="muted">状态：{loginState || "waiting"}</p>
            </div>
          )}

          <form
            className="settings-form"
            style={{ marginTop: 16 }}
            onSubmit={(event) => {
              event.preventDefault();
              void run("pan-import-tokens", async () => {
                await api.panImportTokens({
                  access_token: accessTokenDraft,
                  refresh_token: refreshTokenDraft,
                });
                setAccessTokenDraft("");
                setRefreshTokenDraft("");
                await reloadPan();
                report("OpenList Token 已导入");
              }, "none");
            }}
          >
            <label>
              <span>OpenList 115 access_token</span>
              <input
                type="password"
                autoComplete="off"
                value={accessTokenDraft}
                onChange={(e) => setAccessTokenDraft(e.target.value)}
              />
            </label>
            <label>
              <span>OpenList 115 refresh_token</span>
              <input
                type="password"
                autoComplete="off"
                value={refreshTokenDraft}
                onChange={(e) => setRefreshTokenDraft(e.target.value)}
              />
            </label>
            <p className="muted">Token 来自 OpenList 的 115 Open 存储；使用与 OpenList/Miyabi 相同的社区 client_id 100197303。导入可能使旧 refresh slot 失效（115 约允许 2 个）。</p>
            <button
              type="submit"
              disabled={!accessTokenDraft || !refreshTokenDraft || busy === "pan-import-tokens"}
            >导入 OpenList Token</button>
          </form>

          {panSettings && (
            <form
              className="settings-form"
              style={{ marginTop: 16 }}
              onSubmit={(event) => {
                event.preventDefault();
                void run("pan-settings", async () => {
                  const saved = await api.savePanSettings({
                    ...panSettings,
                    client_id: clientIdDraft.trim(),
                    ...(clientSecretDraft.trim() ? { client_secret: clientSecretDraft.trim() } : {}),
                    offline_directory_id: dirDraft.trim() || null,
                  });
                  setPanSettings(saved);
                  setClientIdDraft(saved.client_id);
                  setClientSecretDraft("");
                  await api.panSetDirectory(dirDraft.trim() || "0").catch(() => undefined);
                  await reloadPan();
                  report("115 设置已保存");
                }, "none");
              }}
            >
              <label>
                <span>115 Open 应用 client_id</span>
                <input
                  value={clientIdDraft}
                  onChange={(e) => setClientIdDraft(e.target.value)}
                  placeholder="100197303（社区临时应用）"
                />
              </label>
              <label>
                <span>可选 client_secret{panSettings.client_secret_set ? "（已设置，留空保持不变）" : ""}</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={clientSecretDraft}
                  onChange={(e) => setClientSecretDraft(e.target.value)}
                  placeholder="不回显已保存 secret"
                />
              </label>
              <p className="muted">切换 client_id 会清除当前 115 凭证，需重新登录或导入 Token。</p>
              <label>
                <span>离线目录 ID（cid）</span>
                <input
                  value={dirDraft}
                  onChange={(e) => setDirDraft(e.target.value)}
                  placeholder="0 为根目录"
                />
              </label>
              <div className="magnet-row" style={{ gridTemplateColumns: "1fr auto auto" }}>
                <input
                  value={browseId}
                  onChange={(e) => setBrowseId(e.target.value)}
                  placeholder="浏览目录 id"
                />
                <button
                  type="button"
                  className="secondary"
                  disabled={!pan?.connected || busy === "pan-browse"}
                  onClick={() => {
                    void run("pan-browse", async () => {
                      const page = await api.panFiles(browseId || "0", 1);
                      setBrowseItems(page.items.filter((item) => item.is_directory));
                      report(`已加载 ${page.items.length} 项`);
                    }, "none");
                  }}
                >浏览</button>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setDirDraft(browseId);
                    report(`已填入目录 ${browseId}`);
                  }}
                >选用当前</button>
              </div>
              {browseItems.length > 0 && (
                <ul className="muted" style={{ maxHeight: 160, overflow: "auto", paddingLeft: 18 }}>
                  {browseItems.map((item) => (
                    <li key={item.id}>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => {
                          setBrowseId(item.id);
                          setDirDraft(item.id);
                        }}
                      >{item.name || item.id}</button>
                      <small> {item.id}</small>
                    </li>
                  ))}
                </ul>
              )}
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.strm_enabled}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_enabled: e.target.checked })}
                />
                离线完成后写 STRM
              </label>
              <label>
                <span>STRM 输出根目录</span>
                <input
                  value={panSettings.strm_output_root ?? ""}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_output_root: e.target.value || null })}
                  placeholder="/media/strm"
                />
              </label>
              <label>
                <span>STRM URL 前缀（OpenList /d/…）</span>
                <input
                  value={panSettings.strm_url_prefix}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_url_prefix: e.target.value })}
                  placeholder="http://openlist:5244/d/115"
                />
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.use_proxy}
                  onChange={(e) => setPanSettings({ ...panSettings, use_proxy: e.target.checked })}
                />
                115 请求走 SHADOW_MDC_PROXY_URL（默认关闭，降低风控）
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.subscription_auto_offline}
                  onChange={(e) => setPanSettings({ ...panSettings, subscription_auto_offline: e.target.checked })}
                />
                订阅 / 想看自动盯磁链并推 115 离线（未登录时仅保存磁链）
              </label>
              <button type="submit" disabled={busy === "pan-settings"}>保存 115 设置</button>
            </form>
          )}

          <h3 style={{ marginTop: 16 }}>最近离线任务</h3>
          {offlineTasks.length === 0
            ? <p className="muted">暂无本地记录的 115 离线任务。</p>
            : (
              <div className="magnet-list">
                {offlineTasks.slice(0, 12).map((task) => (
                  <div className="magnet-row" key={task.id}>
                    <span>
                      {task.remote_name || task.info_hash.slice(0, 12)}
                      {" · "}
                      {task.status}
                      {task.status === "running" ? ` ${Math.round(task.progress)}%` : ""}
                    </span>
                    <small className="muted">{task.work_id.slice(0, 8)}</small>
                  </div>
                ))}
              </div>
            )}
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
                }, "none");
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
      <article className="settings-card">
        <h2>GFriends 女优头像</h2>
        <p className="muted">
          用社区女优头像库补全空的演员写真（只写入真实匹配图，不生成占位图）。支持罗马音/英文名→日文名别名匹配。图片缓存到本机 data/actor-images；可将仍指向 CDN 的地址本地化。
        </p>
        <div className="command-bar-row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
          <button
            type="button"
            className="secondary"
            disabled={busy === "fill-gfriends-dry"}
            onClick={() =>
              void run(
                "fill-gfriends-dry",
                async () => {
                  const result = await api.fillGfriendsActorImages({ dry_run: true, download: false, limit: 500 });
                  report(
                    `预览：扫描 ${result.scanned} · 可匹配 ${result.matched} · 无匹配 ${result.skipped_no_match}（Filetree ${result.filetree_entries}）`
                  );
                },
                "none"
              )
            }
          >
            预览可补全
          </button>
          <button
            type="button"
            disabled={busy === "fill-gfriends"}
            onClick={() =>
              void run(
                "fill-gfriends",
                async () => {
                  const result = await api.fillGfriendsActorImages({ download: true, force_refresh: false });
                  report(
                    `已补全 ${result.filled}/${result.scanned}（下载 ${result.downloaded} · 失败 ${result.failed} · 无匹配 ${result.skipped_no_match}）`
                  );
                },
                "actors"
              )
            }
          >
            补全缺失头像
          </button>
          <button
            type="button"
            className="secondary"
            disabled={busy === "localize-gfriends"}
            onClick={() =>
              void run(
                "localize-gfriends",
                async () => {
                  const result = await api.fillGfriendsActorImages({ localize: true, download: true });
                  report(
                    `CDN→本地 ${result.filled}/${result.scanned}（下载 ${result.downloaded} · 失败 ${result.failed}）`
                  );
                },
                "actors"
              )
            }
          >
            CDN 头像本地化
          </button>
        </div>
      </article>
      <CatalogImportEditor busy={busy} run={run} report={report} />
    </section>
  );
}
