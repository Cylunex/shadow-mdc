import { useEffect, useState } from "react";
import { api } from "../api";
import { FieldPrioritySettings } from "../components/FieldPrioritySettings";
import { IdentifyByUrlPanel } from "../components/IdentifyByUrlPanel";
import type { Library, MediaServerSettings, OpenListTest, PanOfflineTask, PanSettings, PanStatus } from "../model";
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
  // null = keep the stored STRM token; "" = clear; other = new token.
  const [strmTokenDraft, setStrmTokenDraft] = useState<string | null>(null);
  const [accessTokenDraft, setAccessTokenDraft] = useState("");
  const [refreshTokenDraft, setRefreshTokenDraft] = useState("");
  const [browseId, setBrowseId] = useState("0");
  // OpenList backend drafts. Secrets are write-only: "" in a draft means "keep".
  const [olTokenDraft, setOlTokenDraft] = useState("");
  const [olUserDraft, setOlUserDraft] = useState("");
  const [olPasswordDraft, setOlPasswordDraft] = useState("");
  const [olTest, setOlTest] = useState<OpenListTest | null>(null);
  const [olBrowsePath, setOlBrowsePath] = useState("/");
  const [olBrowseItems, setOlBrowseItems] = useState<Array<{ name: string; path: string }>>([]);
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
      setOlUserDraft(settings.openlist_username ?? "");
      if (settings.openlist_offline_path) setOlBrowsePath(settings.openlist_offline_path);
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
          {panSettings && (
            <label>
              <span>网盘后端</span>
              <select
                value={panSettings.pan_backend}
                onChange={(e) => {
                  const pan_backend = e.target.value as "115_open" | "openlist";
                  void run("pan-backend", async () => {
                    const saved = await api.savePanBackend({ pan_backend });
                    setPanSettings(saved);
                    setOlTest(null);
                    await reloadPan();
                    report(pan_backend === "openlist" ? "已切换到 OpenList 后端" : "已切换到 115 Open 后端");
                  }, "none");
                }}
                disabled={busy === "pan-backend"}
              >
                <option value="115_open">115 Open 平台（二维码登录 / Token 导入）</option>
                <option value="openlist">OpenList（使用已挂载 115 的 OpenList）</option>
              </select>
            </label>
          )}
          <p className="muted">
            {pan
              ? `${pan.provider} · ${pan.available ? "可用" : "不可用"} — ${pan.reason}`
              : "正在读取 /api/pan/status…"}
          </p>
          {pan?.needs_relogin && (
            <p className="danger-text" role="alert">
              需要重新登录：{pan.backend === "openlist" ? "OpenList 拒绝了已保存的令牌 / 密码，已清除，请重新填写凭据。" : "115 拒绝了已保存的登录，已清除，请重新扫码登录。"}
            </p>
          )}
          {pan?.account && (
            <p className="muted">
              账号 {pan.account.user_name || pan.account.user_id || "已连接"}
              {pan.account.expires_at ? ` · token 至 ${pan.account.expires_at}` : ""}
            </p>
          )}
          {panSettings?.pan_backend === "openlist" && (
            <form
              className="settings-form"
              style={{ marginTop: 12 }}
              onSubmit={(event) => {
                event.preventDefault();
                void run("openlist-save", async () => {
                  const saved = await api.savePanBackend({
                    openlist_base_url: panSettings.openlist_base_url?.trim() || null,
                    openlist_strm_base_url: panSettings.openlist_strm_base_url?.trim() || null,
                    openlist_offline_path: panSettings.openlist_offline_path?.trim() || null,
                    openlist_offline_tool: panSettings.openlist_offline_tool.trim() || "115 Cloud",
                    openlist_delete_policy: panSettings.openlist_delete_policy,
                    openlist_strm_sign: panSettings.openlist_strm_sign,
                  });
                  const secrets: { token?: string; username?: string; password?: string } = {};
                  if (olTokenDraft.trim()) secrets.token = olTokenDraft.trim();
                  if (olUserDraft.trim() !== (saved.openlist_username ?? "")) secrets.username = olUserDraft.trim();
                  if (olPasswordDraft) secrets.password = olPasswordDraft;
                  if (Object.keys(secrets).length > 0) await api.saveOpenListCredentials(secrets);
                  setOlTokenDraft("");
                  setOlPasswordDraft("");
                  await reloadPan();
                  report("OpenList 设置已保存");
                }, "none");
              }}
            >
              <p className="roadmap-note">
                离线下载、目录遍历和 STRM 全部通过 OpenList API 完成，115 凭证只保存在 OpenList 里。
                OpenList 需已挂载 115 存储（115 Cloud 或 115 Open 驱动），离线工具名需与挂载驱动对应。
              </p>
              <label>
                <span>OpenList 地址（本服务访问用）</span>
                <input
                  value={panSettings.openlist_base_url ?? ""}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_base_url: e.target.value || null })}
                  placeholder="http://192.168.0.2:5244"
                />
              </label>
              <label>
                <span>API Token{panSettings.openlist_token_set ? "（已设置，留空保持不变）" : ""}</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={olTokenDraft}
                  onChange={(e) => setOlTokenDraft(e.target.value)}
                  placeholder="OpenList 管理 → 设置 → 其他 → 令牌；或下方填用户名/密码"
                />
              </label>
              <div className="magnet-row" style={{ gridTemplateColumns: "1fr 1fr" }}>
                <input
                  value={olUserDraft}
                  autoComplete="off"
                  onChange={(e) => setOlUserDraft(e.target.value)}
                  placeholder="用户名（可选）"
                />
                <input
                  type="password"
                  autoComplete="off"
                  value={olPasswordDraft}
                  onChange={(e) => setOlPasswordDraft(e.target.value)}
                  placeholder={panSettings.openlist_password_set ? "密码已设置，留空保持" : "密码（可选）"}
                />
              </div>
              <label>
                <span>离线目标路径（OpenList 内 115 挂载下的目录）</span>
                <input
                  value={panSettings.openlist_offline_path ?? ""}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_offline_path: e.target.value || null })}
                  placeholder="/115/云下载"
                />
              </label>
              <div className="magnet-row" style={{ gridTemplateColumns: "1fr auto auto" }}>
                <input value={olBrowsePath} onChange={(e) => setOlBrowsePath(e.target.value)} placeholder="浏览路径" />
                <button
                  type="button"
                  className="secondary"
                  disabled={!pan?.connected || busy === "openlist-browse"}
                  onClick={() => {
                    void run("openlist-browse", async () => {
                      const page = await api.openListFiles(olBrowsePath || "/", 1);
                      setOlBrowseItems(page.items.filter((item) => item.is_directory));
                      report(`已加载 ${page.items.length} 项`);
                    }, "none");
                  }}
                >浏览</button>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => setPanSettings({ ...panSettings, openlist_offline_path: olBrowsePath })}
                >选用当前</button>
              </div>
              {olBrowseItems.length > 0 && (
                <ul className="muted" style={{ maxHeight: 160, overflow: "auto", paddingLeft: 18 }}>
                  {olBrowseItems.map((item) => (
                    <li key={item.path}>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => {
                          setOlBrowsePath(item.path);
                          setPanSettings({ ...panSettings, openlist_offline_path: item.path });
                        }}
                      >{item.name}</button>
                    </li>
                  ))}
                </ul>
              )}
              <label>
                <span>离线工具（tool）</span>
                <input
                  list="openlist-tools"
                  value={panSettings.openlist_offline_tool}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_offline_tool: e.target.value })}
                  placeholder="115 Cloud"
                />
                <datalist id="openlist-tools">
                  <option value="115 Cloud" />
                  <option value="115 Open" />
                </datalist>
              </label>
              <label>
                <span>删除策略（delete_policy）</span>
                <select
                  value={panSettings.openlist_delete_policy}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_delete_policy: e.target.value })}
                >
                  <option value="delete_on_upload_succeed">delete_on_upload_succeed（默认）</option>
                  <option value="delete_on_upload_failed">delete_on_upload_failed</option>
                  <option value="delete_never">delete_never</option>
                  <option value="delete_always">delete_always</option>
                </select>
              </label>
              <label>
                <span>STRM 中的 OpenList 地址（Emby 访问用，可选）</span>
                <input
                  value={panSettings.openlist_strm_base_url ?? ""}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_strm_base_url: e.target.value || null })}
                  placeholder="留空 = 与上面的 OpenList 地址相同"
                />
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.openlist_strm_sign}
                  onChange={(e) => setPanSettings({ ...panSettings, openlist_strm_sign: e.target.checked })}
                />
                /d 链接附带 ?sign=（OpenList 开启签名时勾选）
              </label>
              <div className="command-bar-row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
                <button type="submit" disabled={busy === "openlist-save"}>保存 OpenList 设置</button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy === "openlist-test"}
                  onClick={() => {
                    void run("openlist-test", async () => {
                      const result = await api.testOpenList();
                      setOlTest(result);
                      report(result.ok ? `OpenList 连接正常：${result.detail ?? ""}` : `OpenList 连接失败：${result.detail ?? ""}`);
                    }, "none");
                  }}
                >测试连接</button>
                <button
                  type="button"
                  className="ghost"
                  disabled={busy === "openlist-clear"}
                  onClick={() => {
                    void run("openlist-clear", async () => {
                      await api.clearOpenListCredentials();
                      setOlUserDraft("");
                      await reloadPan();
                      report("已清除 OpenList 凭证");
                    }, "none");
                  }}
                >清除凭证</button>
              </div>
              {olTest && (
                <p className="muted">
                  {olTest.ok ? "✓" : "✗"} 用户 {olTest.user ?? "—"}
                  {olTest.target_path ? ` · 目标 ${olTest.target_path}：${olTest.target_ok ? `可访问（${olTest.target_entries ?? 0} 项${olTest.target_writable === false ? "，无写权限" : ""}）` : "不可访问"}` : ""}
                  {olTest.tools ? ` · 可用工具 ${olTest.tools.join(" / ")}` : ""}
                  {olTest.detail ? ` — ${olTest.detail}` : ""}
                </p>
              )}
            </form>
          )}
          {panSettings?.pan_backend !== "openlist" && (<>
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
          </>)}

          {panSettings && (
            <form
              className="settings-form"
              style={{ marginTop: 16 }}
              onSubmit={(event) => {
                event.preventDefault();
                void run("pan-settings", async () => {
                  const openlistMode = panSettings.pan_backend === "openlist";
                  const saved = await api.savePanSettings({
                    ...panSettings,
                    ...(openlistMode ? {} : { client_id: clientIdDraft.trim() }),
                    ...(!openlistMode && clientSecretDraft.trim() ? { client_secret: clientSecretDraft.trim() } : {}),
                    ...(strmTokenDraft !== null ? { strm_token: strmTokenDraft.trim() } : {}),
                    offline_directory_id: openlistMode ? panSettings.offline_directory_id : dirDraft.trim() || null,
                  });
                  setPanSettings(saved);
                  setStrmTokenDraft(null);
                  setClientIdDraft(saved.client_id);
                  setClientSecretDraft("");
                  if (!openlistMode) await api.panSetDirectory(dirDraft.trim() || "0").catch(() => undefined);
                  await reloadPan();
                  report(openlistMode ? "STRM / 订阅设置已保存" : "115 设置已保存");
                }, "none");
              }}
            >
              {panSettings.pan_backend !== "openlist" && (<>
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
              </>)}
              {panSettings.strm_enabled && panSettings.strm_config_errors.length > 0 && (
                <div className="danger-text" role="alert">
                  <strong>STRM 设置不完整，已暂停导出和重写（不会写出半配置的媒体库）：</strong>
                  <ul>
                    {panSettings.strm_config_errors.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </div>
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
                <span>STRM 模式</span>
                <select
                  value={panSettings.strm_mode}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_mode: e.target.value as "openlist" | "relay" })}
                >
                  <option value="openlist">
                    {panSettings.pan_backend === "openlist" ? "OpenList /d 直链（{OpenList 地址}/d{路径}）" : "OpenList /d 前缀（原有方式）"}
                  </option>
                  <option value="relay">
                    {panSettings.pan_backend === "openlist" ? "本服务 302 中转 → OpenList /d 链接" : "本服务 302 中转 /api/strm/play/{file_id}"}
                  </option>
                </select>
              </label>
              {panSettings.strm_mode === "openlist" && panSettings.pan_backend === "openlist" ? null : panSettings.strm_mode === "openlist" ? (
                <label>
                  <span>STRM URL 前缀（OpenList /d/…）</span>
                  <input
                    value={panSettings.strm_url_prefix}
                    onChange={(e) => setPanSettings({ ...panSettings, strm_url_prefix: e.target.value })}
                    placeholder="http://openlist:5244/d/115"
                  />
                </label>
              ) : (
                <>
                  <label>
                    <span>对外地址（Emby/播放器访问本服务的 URL）</span>
                    <input
                      value={panSettings.strm_public_base_url ?? ""}
                      onChange={(e) => setPanSettings({ ...panSettings, strm_public_base_url: e.target.value || null })}
                      placeholder="http://192.168.0.21:8700"
                    />
                  </label>
                  <label>
                    <span>STRM 令牌（可选，附加 ?token=）{panSettings.strm_token_set ? " · 已设置" : ""}</span>
                    <div className="magnet-row" style={{ gridTemplateColumns: "1fr auto auto" }}>
                      <input
                        value={strmTokenDraft ?? ""}
                        onChange={(e) => setStrmTokenDraft(e.target.value)}
                        placeholder={panSettings.strm_token_set ? "留空保持现有令牌" : "未设置（仅建议局域网使用）"}
                      />
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => {
                          const bytes = new Uint8Array(18);
                          window.crypto.getRandomValues(bytes);
                          setStrmTokenDraft(Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(""));
                        }}
                      >随机生成</button>
                      <button type="button" className="ghost" onClick={() => setStrmTokenDraft("")}>清除</button>
                    </div>
                  </label>
                  <label>
                    <span>115 请求 User-Agent（播放器未带 UA 时使用）</span>
                    <input
                      value={panSettings.strm_user_agent ?? ""}
                      onChange={(e) => setPanSettings({ ...panSettings, strm_user_agent: e.target.value || null })}
                      placeholder="默认 Chrome UA"
                    />
                  </label>
                </>
              )}
              <label>
                <span>Emby 看到的 STRM 根目录（容器路径，用于通知）</span>
                <input
                  value={panSettings.strm_emby_root ?? ""}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_emby_root: e.target.value || null })}
                  placeholder="开启 Emby 通知时必填（路径相同也要填）"
                />
              </label>
              <label>
                <span>删除对账间隔（小时，0 关闭）</span>
                <input
                  type="number"
                  min={0}
                  max={720}
                  value={panSettings.strm_reconcile_interval_hours}
                  onChange={(e) => setPanSettings({ ...panSettings, strm_reconcile_interval_hours: Number(e.target.value) || 0 })}
                />
              </label>
              <div className="command-bar-row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy === "strm-rewrite"}
                  onClick={() => {
                    void run("strm-rewrite", async () => {
                      const result = await api.strmRewrite();
                      report(`STRM 重写：扫描 ${result.scanned}，改写 ${result.rewritten}，跳过 ${result.skipped}`);
                    }, "none");
                  }}
                >按当前设置重写 STRM</button>
                <button
                  type="button"
                  className="ghost"
                  disabled={!pan?.connected || busy === "strm-reconcile"}
                  onClick={() => {
                    void run("strm-reconcile", async () => {
                      const result = await api.strmReconcile();
                      report(result.started ? "已开始删除对账（后台运行）" : "对账/重写正在进行中");
                    }, "none");
                  }}
                >立即删除对账</button>
              </div>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.use_proxy}
                  onChange={(e) => setPanSettings({ ...panSettings, use_proxy: e.target.checked })}
                />
                115 请求走 SHADOW_MDC_PROXY_URL（默认关闭，降低风控；OpenList 后端始终直连）
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={panSettings.subscription_auto_offline}
                  onChange={(e) => setPanSettings({ ...panSettings, subscription_auto_offline: e.target.checked })}
                />
                订阅 / 想看自动盯磁链并推离线（115 Open 或 OpenList，未配置时仅保存磁链）
              </label>
              <button type="submit" disabled={busy === "pan-settings"}>
                {panSettings.pan_backend === "openlist" ? "保存 STRM / 订阅设置" : "保存 115 设置"}
              </button>
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
                      {task.backend === "openlist" ? "[OpenList] " : ""}
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
                <span>STRM 变更通知防抖（秒，批量调用 Library/Media/Updated）</span>
                <input
                  type="number"
                  min={0}
                  max={600}
                  value={media.notify_debounce_seconds ?? 5}
                  onChange={(e) => setMedia({ ...media, notify_debounce_seconds: Number(e.target.value) || 0 })}
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
