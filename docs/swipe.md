# 刷流（Swipe / Tube）

Shadow MDC 内嵌 **外站 tube 刷流** + **本库竖滑**，同一 OIDC 会话，不另开独立 `/swipefeed` 应用（遗留 `swipefeed-web` 可暂留对照）。

## 架构

| 层 | 说明 |
|---|---|
| SPA `?view=swipe` / `/mdc/swipe/` | 全屏竖滑：默认「外站」tube；可切「本库」 |
| FastAPI `/api/swipe/*` | 反向代理到 Node sidecar（`SHADOW_MDC_SWIPE_UPSTREAM_URL`，默认 `http://127.0.0.1:8791`） |
| Sidecar `services/swipefeed/` | 原 swipefeed-web 精简：`feed` / `search` / `resolve` / `hls` / `stream` / `sites` / circuit |
| 站点配置 | `SWIPEFEED_DATA_DIR`（NAS：`shared/data/swipefeed/site-settings.json`），**永不进 Git** |
| 播放 | 共用 `InAppPlayer`：外站 m3u8/mp4 经 `/api/swipe/hls|stream` 代理 Referer/CORS；本库 playable 源走同一路径，换源不拆播放器 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHADOW_MDC_SWIPE_UPSTREAM_URL` | `http://127.0.0.1:8791` | sidecar 根 URL；空则 `/api/swipe/health` 报 unset |
| `SHADOW_MDC_SWIPE_PROXY_TIMEOUT_SECONDS` | （config） | 代理超时 |
| Sidecar `PORT` | `8791` | supervisor 模板 |
| Sidecar `SWIPEFEED_BROWSER_API_PREFIX` | `/api/swipe` | 改写 playlist 里的媒体 URL 前缀 |
| Sidecar `SWIPEFEED_DATA_DIR` | 见 supervisor | 本地 site-settings / sessions |

## API（浏览器，经 mdc）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/swipe/health` | sidecar 可达性 |
| GET | `/api/swipe/feed` | tube 列表 |
| POST | `/api/swipe/resolve` | 单条解析直链 |
| GET | `/api/swipe/hls` / `/stream` | 媒体代理（Query `u` + 可选 `r` Referer） |
| GET | `/api/swipe/sites` 等 | 站点/设置/circuit（与 sidecar 一致） |

本库播放仍用 `GET /api/works/{id}/play`；可播 m3u8 由前端 `swipeMediaProxyUrl` 包一层再进 `InAppPlayer`。

## 部署

`scripts/nas/deploy-mdc.sh` 会：

1. `npm ci` 安装 `services/swipefeed` 依赖  
2. 安装/更新 `/etc/supervisor/conf.d/shadow-mdc-swipefeed.conf`  
3. 确保 `shared/data/swipefeed/`；若无 `site-settings.json` 则从遗留 `/data/project/swipefeed-web/data/` 拷贝  
4. 重启 sidecar 并探测 `/api/swipe/health`

首次部署请在 `/data/project/.secrets/shadow-mdc/environment` 增加：

```bash
SHADOW_MDC_SWIPE_UPSTREAM_URL=http://127.0.0.1:8791
```

（未设时 Python 默认值已是该地址。）

## 合规

- 成人白名单 + 未成年/李宗瑞等排除：沿用 playback / 站内规则；勿新增 VIP/解锁绕过。  
- 真实站点 URL、cookie、token **只**放 NAS/本机 local 文件。  
- AUTH/OIDC 与主站相同；MCP Bearer 仍仅 `/api/mcp/v1`。

## 与独立 swipefeed-web

生产可继续跑 `:8790` `/swipefeed/` 作对照；新入口是 `/mdc/swipe/`。确认稳定后可再停独立应用。
