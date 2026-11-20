# MDC 在线影视与 TVBox 源管理

从 MDC 主导航进入 **TVBox**。观看页提供海报目录、最近更新、仓库和站点切换、分类、跨站搜索、收藏及继续观看。影片详情按线路展示分集。选择分集后在原页播放，可用浏览器原生控制全屏、拖动和调速。连播、线路切换和历史恢复使用稳定的分集与线路身份。

## 导入与维护

在「管理来源」粘贴 CMS 或多仓地址，也可上传 TVBox JSON/JSONC 或 Relay 导出包。点一次「导入并测试」。MDC 逐站抽样查询分类、影片详情和 HLS/MP4 媒体响应。通过的站点自动进入观看目录和可发布范围，失败项可在回执中单独重试。

| 输入 | 行为 |
| --- | --- |
| type 1 CMS | 读取 JSON 分类、分页、搜索、详情和分集。处理无文件后缀的 HLS/MP4 |
| type 0 CMS | 读取 XML 的 class、list 和 dl，按 XML 站点约定请求 videolist |
| 多仓配置 | 递归展开 urls，限制嵌套深度、请求数和响应体积。相对地址按配置位置解析 |
| Relay v1 数据包或私有迁移清单 | 恢复 VOD 源、修订、源组、绑定和原始上游。重新测试生效站点 |
| 运行时扩展 | JAR、CSP、脚本和网页嗅探记录为兼容性问题。Relay 直播条目保存在归档记录，直播播放仍走 Media 的 IPTV 入口 |

站点可启停、隐藏、改名、调优先级、手动复测、固定或切换历史修订。URL 配置按原始入口更新，可在「配置更新与迁移记录」立即更新。新修订测试失败时沿用上一可用分类快照。按站点分类 ID 过滤目录、详情和解析，源的调整存于私有 SQLite。完整测试会校验样本的媒体响应，具体影片在用户点播时重新解析。

Losna 和命令行共用 mdc-operator 的 `tvbox.import`、`tvbox.status`、`tvbox.sources`、`tvbox.sync`，通过服务 API 操作。

## Web 与 Media 播放

浏览器对选定分集调用 `POST /api/tvbox/sites/{site_id}/items/{item_id}/watch`。MDC 向播放页提供同源的短时资源标识。服务端转发选中线路所需的播放头，按 HLS 的主清单、子清单、分片、密钥和初始化段逐级解析，将它们映射到本次播放会话内。MP4 通过 HTTP Range 边读边传。对每跳连接实施公网目的地址校验和连接、清单大小限制。站点停用后拒绝新资源请求。浏览器记录最近观看分集及播放位置，不持久化播放地址。

Shadow Media 在现有 MDC 连接内用 `tvbox_v1` 发现点播能力，用 `tvbox_groups` 发现仓库。可按仓库浏览或搜索，选择分集后交给原生播放器。同一分集线路用稳定 ID 恢复。临时 URL 失效时可按原 ID 重新解析。Android 继续使用手动 MDC 令牌或 Novel 统一账号，播放请求使用独立的媒体连接。

## 影视仓与 TVBox 订阅

在 Web「管理来源」创建分组后，按全部仓库或指定仓库签发订阅。可复制 `store.json` 多仓地址，也可复制 `sites.json` 单仓地址。多仓入口生成固定版本子地址，新访问稳定地址时自动反映已经启用的站点。发布范围支持有效期、重签和停用。旧版本可回读，遇到已撤回成员或已失效绑定时关闭访问。

服务端只保存订阅口令摘要。生产接入时只需要将 `/api/tvbox-feed/` 这条凭地址授权的读取路径配置给第三方客户端，源管理和浏览器播放继续使用 MDC 会话。访问日志应对订阅路径中的凭据脱敏。代理要保留实际对外地址及子路径，供多仓清单生成子地址。

## Relay 迁移

在导入表单选 Relay v1 的 `.tar.gz` 或 `.tgz`，也支持经 `scripts/prepare_relay_tvbox.py --takeover` 生成的私有 JSON。MDC 筛选 TVBox 相关对象，保留生效和历史修订、TVBox 成员优先顺序、可映射的隐藏/置顶/改名偏好、发布、体检和旧绑定。无旧摘要的绑定显示为「待重签」。旧档中的 TVBox 生效站点经当前网络和播放校验后直接启用。重复导入复用同一迁移标识，保留已在 MDC 调整的分组。旧修订可从管理页重新测试。

2026-11-20 使用 Relay 管理 API 摘取 TVBox 对象，在临时 SQLite 实际导入了 23 个源包、398 个修订、24,068 条体检、19 个候选、1 个仓库、2 个发布和 1 个待重签绑定。32 个 CMS 站点中本机有 3 个通过当前播放抽样，主要失败原因为上游连接超时。真实数据保存在工作区外置私有运行资料。生产切换时须复核剩余非 TVBox 绑定的承接安排。

## 参考项目

- FongMi TV 的站点类型、能力项以及分线路详情作为 TVBox 协议依据。[配置文档](https://github.com/FongMi/TV/blob/fongmi/website/app/config/page.tsx) · [点播接口](https://github.com/FongMi/TV/blob/fongmi/website/app/spider/page.tsx)
- TVBox Web 的多源搜索、分集切换和本机续播作为 Web 观看交互参考。LibreTV 用于核对搜索到播放器的最短流程。[TVBox Web](https://github.com/silverbullet-liang/tvbox-web) · [LibreTV](https://github.com/LibreSpark/LibreTV)
- TVBox-Suite 的配置批量维护，以及 Lightconer 项目同时发布单仓和多仓的做法作为分组及订阅参考。[TVBox-Suite](https://github.com/zhiyuan411/TVBox-Suite) · [影视仓配置聚合](https://github.com/Lightconer/tvbox-ysc-config)
- Stremio 和 Kodi 分别提供资源流请求与目录、播放解析的契约对照。MDC 在用户选集时解析流，Media 可以用同一分集标识重取。[Stremio SDK](https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/api/requests/defineStreamHandler.md) · [Kodi](https://xbmc.github.io/docs.kodi.tv/master/kodi-dev-kit/group__python__xbmcplugin.html)
