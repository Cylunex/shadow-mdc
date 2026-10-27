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

作品详情「磁力」面板：`打开 Emby` + `在线播放`。可播源（VOD m3u8 / 直链 mp4 等）优先 **页内 `InAppPlayer`**（换源不拆播放器；失败自动切下一健康源并 toast）；仅不可应用内播放的深链才新标签。外站媒体经 `/api/swipe/hls|stream` 代理，避免浏览器 ORB/CORS。

### 本库作品续播

详情页与本库刷流共用以作品 ID 标识的浏览器播放进度。关闭播放器、切换页面或刷新播放地址后，
重新播放会在媒体元数据可用时恢复位置；自动换源优先沿用当前播放位置。
不足 5 秒或距结尾不足 10 秒的位置不作为下一次续播点，播完清除记录。
若新片源短于记录位置，则从头播放，避免越界。

进度仅保存在当前浏览器的 `localStorage`，不新增服务器请求，也不记录媒体 URL、凭据或站点。
最多保留 200 个作品，超过 90 天的记录不再使用，下一次保存时清理；时间更新写入最多每 5 秒一次，
暂停、跳转、离页及卸载时补存。清除网站数据会清除续播；禁用存储时正常播放。
外站刷流与外部播放器不使用此记录，也不在设备间同步。

### 刷流（Swipe）

- 顶栏 **刷流**，或 `/?view=swipe` / `/mdc/swipe/`。
- **外站** tube（sidecar）与 **本库** 竖滑同一 SPA；详见 [swipe.md](swipe.md)。
- 本库：在线播放走同一 `InAppPlayer` + play resolve（`chosen` / `failures` / `probe_ok`）；入库 / 详情仍可用。
- 与主 UI 同源 OIDC；片源 / tube 站点名单仅 NAS local-config，**勿**提交真实站点。

