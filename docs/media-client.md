# Media 原生片库与 Emby 播放

移动端契约位于 `/api/media/v1`。分页查询先限制作品集合再序列化，按入库时间与作品 ID 降序排列，游标绑定检索词。支持番号、标题与演员的子串检索，每页最多 100 条。此检索按入库时间排序。

## 连接

在仓库外运行数据目录的既有 `media-server.json` 中添加 `media_client_token`。示例片段如下，需合并到已有配置。

```json
{
  "enabled": true,
  "kind": "emby",
  "base_url": "https://example.com",
  "api_key": "REPLACE_EMBY_SERVER_TOKEN",
  "media_client_token": "REPLACE_RANDOM_MOBILE_TOKEN"
}
```

令牌使用高熵随机值，实际文件放在既定秘密存储边界并设为 0600。移动端只填写服务地址与移动端令牌，Emby 服务端 API Key 留在 MDC。现有 Web 设置保存会保留移动端令牌。

所有移动端接口与封面都要求 `Authorization: Bearer REPLACE_RANDOM_MOBILE_TOKEN`。未配置移动端令牌返回 503，错误令牌返回 401。此令牌授权读取片库及维护播放关联。既有管理 API 沿用原来的网络和网关保护，外部移动访问的网关应只开放该契约路径，不应开放管理 API。现有浏览器登录网关若拦截 Bearer 请求，需要按此窄路径配置访问。

Media 中从「影视 → 媒体库 → Shadow MDC 片库」添加连接。设置页也有入口。HTTPS 为默认选择，HTTP 需显式启用。连接凭据使用独立 Android Keystore 存储。MDC 目录独立浏览，不加入全局聚合搜索。

## 直接播放

`POST /works/{id}/resolve` 返回结构化 `server_id` 和 `item_id`，由 Media 使用已保存的同服务器 Emby 用户登录播放。已关联作品按条目 ID 验证后直接返回。Emby 404 时重新匹配，其他错误保留原关联。首次匹配要求作品番号完整边界命中且候选唯一，多个版本或分片不自动挑选。

没有番号或有多个候选时，在作品详情选择「选择或更换 Emby 影片」，搜索并选一次。候选接口只返回 ID 和标题，选择后用 `PUT /works/{id}/binding` 保存，目标必须为 Emby 上存在的视频条目。关联是服务端全局作品关联，播放权限、进度和时长按 Media 中实际登录的账号及 Emby 条目处理。多账号时优先当前已选账号，不能唯一决定时要求先选择 Emby 账号。

当前每部作品保存一个 Emby 条目。多个版本、分片可通过更换关联选择，尚无版本列表；直接 HTTP/HLS 回退、跨设备 MDC 进度、演员导航与识别证据工作台仍属于后续设计。只有关联到 Emby 的影片进入本轮播放闭环。

## 验证范围

自动回归覆盖鉴权、静态库分页、检索、番号边界、持久关联复用、无番号手动关联与旧设置保存兼容。客户端回归检查代理前缀、稳定 ID、游标、POST 解析以及重定向不携带移动端令牌。上线前还需在真实 Emby 与手机验证首帧、续播、Seek、无权限条目和返回 MDC 详情。
