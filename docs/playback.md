# 在线播放（Emby + 本地片源配置）

Shadow MDC 可为作品解析播放入口：**Emby/Jellyfin 深链**（公开代码）与 **外站 / Apple-CMS VOD m3u8**（片源列表仅存本地或 NAS，**不进 Git**）。

## 行为概要

1. **媒体库优先**：若已配置 Emby/Jellyfin 且 Items 搜索命中，首选打开媒体库条目深链（或 App Scheme）。
2. **外站跳转**：按本地 `playback-sites.local.json` 的 `external_sites[].url_template` 用番号展开 URL，在新标签打开。
3. **VOD m3u8**：对配置的 Apple-CMS `provide/vod` 用 `ac=detail&wd=番号` 搜索；仅保留 **白名单 type_id** 且分类名未命中未成年暗示词的结果；应用内 HLS 页播放（hls.js），**不代传媒体字节**。
4. **有界多源 resolve**（LumenBox 思路重写）：整次解析有总预算 `resolve_budget_ms`（默认 12s）；顺序 Emby → 外站模板 → VOD；单源探针失败（超时 / 非 200 / 清单非法）记入 `failures` 并跳过下一源，已命中源仍返回。响应含 `chosen`（= preferred）、`failures`、`resolve_ms`，片源可带 `probe_ok` / `probe_reason`。
5. **m3u8 完整性门闩**：把 VOD URL 标为可播前，有界拉取清单（`m3u8_max_bytes` / `m3u8_probe_timeout_seconds`）；须以 `#EXTM3U` 开头且含 `#EXTINF` 或 `#EXT-X-STREAM-INF`；截断 / 空体 / HTML 错误页视为失败，**不当成功缓存**。同一门闩供 resolve 与应用内播放预检复用。
6. 未配置片源文件时，仅 Emby 搜索链可用（与原先「打开 Emby」一致）。

## 配置

| 项 | 说明 |
|---|---|
| `SHADOW_MDC_PLAYBACK_SITES_PATH` | 片源 JSON 绝对路径；未设则用 `{data_dir}/playback-sites.local.json` |
| Emby 设置 | 设置页「Emby / Jellyfin」卡片（`data/media-server.json`） |

Schema 示例（示例域名为 `example.invalid`，**勿提交真实站点**）：见 [dev/playback-sites.example.json](dev/playback-sites.example.json)。

部署时用 `scp`/`rsync` 把本机 `data/playback-sites.local.json` 拷到 NAS `shared/data/`，**不要**经 git push。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/works/{id}/emby-link` | 仅媒体库搜索深链（兼容旧 UI） |
| GET | `/api/works/{id}/play` | 完整解析：preferred + sources |
| GET | `/api/works/{id}/player` | 应用内 HLS 播放页 |
| GET | `/api/mcp/v1/play/{code}` | MCP：按番号解析（需 MCP token） |

## 安全与合规

- 片源 URL / API base **禁止**写入仓库、PR、MCP 描述或公开文档。
- 运行时与探测阶段均过滤分类名命中萝莉/童颜/学生校园/李宗瑞等暗示词的 type；VOD 须配置 `whitelist_type_ids`。
- GPL/AGPL 参考项目只借思路，不拷代码。

## UI

作品详情「磁力」面板：`打开 Emby` + `在线播放`（后者走 `/play`，有 m3u8 时开播放页）。

### 片库刷流（Swipe）

- 顶栏 **刷流** 标签，或深链 `/?view=swipe`；入口页 `/swipe/`（nginx 下为 `/mdc/swipe/`）相对跳转到该视图。
- 竖滑（`scroll-snap`）浏览本库作品：海报 / 番号 / 标题；**在线播放** 调 `GET /api/works/{id}/play`（与详情页相同 resolve），可播则开 `/api/works/{id}/player`；**详情** 进作品页。
- 与主 UI 同源，复用 nginx OIDC cookie/session；**不**走 MCP Bearer。LAN 无 OIDC 时用 MCP 请仍只打 `/api/mcp/v1/*`（见 [mcp.md](mcp.md)）。
- 外站 tube 刷流仍在独立 `swipefeed-web`；片源名单仅 NAS/本机 local-config，**勿**提交真实站点 URL/token。

