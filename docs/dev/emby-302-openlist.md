# Emby 302 / OpenList `/d` 播放硬化说明

借鉴 TgtoDrive「Emby 反代 → 302 → 网盘 CDN」的流量路径，shadow-mdc **不**内置 Emby 反代端口，而是：

1. STRM 正文写入 OpenList 直链 `{openlist}/d{path}[?sign=…]`，或
2. STRM 正文写入本服务中继 `/api/strm/openlist/{path}` / `/api/strm/play/{file_id}`，由服务端再 302。

## 推荐拓扑（OpenList 后端）

```text
客户端 / Emby
  → 读本地 .strm（几 KB）
  → OpenList /d… 或 shadow-mdc /api/strm/openlist/…
  → HTTP 302 Location = 115 CDN
  → 视频字节不经过 NAS 上行
```

## 必填项

| 项 | 说明 |
| --- | --- |
| `openlist_base_url` / `openlist_strm_base_url` | Emby **客户端**能解析到的 OpenList 公网/内网地址；容器内 `http://openlist:5244` 对电视盒子无效 |
| `openlist_offline_path` | 离线目标；中继只允许该树下的路径（防任意文件 302） |
| `strm_emby_root` | 开启媒体服务器通知时，Emby 容器内看到的 STRM 根 |
| `media_server.enabled` + `api_key` | 仍关闭时，离线→STRM→NFO 照常写盘，只是不推 `Library/Media/Updated` |
| `media_server.base_url` | Emby **API** 地址（如 `http://192.168.0.21:8096`）。播放用的 Emby302 反代（如 `:8099`）不要填这里 |


## 排错

1. 播放打满 NAS 上行：STRM 是否仍指向本机中转且中转在拉流（应只回 302，不代传 body）。
2. 403 / 失效：开启 `openlist_strm_sign` 时用中继模式，让签名在播放时刷新。
3. 404 path outside offline target：导出路径不在 `openlist_offline_path` 下——把离线根调到媒体库根，或改路径。
4. Emby 不刮削：查 `/api/pan/pipeline` 的 `stages.nfo` / `stages.emby.needs`；缺 API Key 时 `needs` 会列出。

## 刻意不做

- 不迁移到 TgtoDrive，不部署其 host-network Emby 反代。
- 不接 Telegram Bot；分享链摄入走 `POST /api/pan/offline/intake`（番号绑定）。
