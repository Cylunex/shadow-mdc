# 参考与取舍

提交: beb3400

Shadow MDC 的代码与数据模型从零实现。下列项目只用于确认常见工作流、部署形态和生态接口，不复制其源码或界面结构。

- [JavSP](https://github.com/Yuukiy/JavSP)、[MDCX](https://github.com/sqzw-x/mdcx)：番号扫描、NFO 与本地整理的基础场景。
- [OpenAver](https://github.com/slive777/OpenAver)：来源状态、人工浏览和本地桌面体验。
- [Javinizer Go](https://github.com/javinizer/javinizer-go)：CLI、Web、容器和预览式整理。
- [Stash](https://github.com/stashapp/stash) 与 [CommunityScrapers](https://github.com/stashapp/CommunityScrapers)：场景模型、指纹候选与站点适配器边界。
- [FSS](https://github.com/Anastylosis/FSS)：片商目录增量导入，适合作为独立 catalogue provider。
- [mdcx-diy](https://github.com/cdlongbow/mdcx-diy)：来源优先级、字段合并和中文别名思路；未验证为探花专用来源。
- [AVScan](https://avscan.cc/about/)：仅作为用户主动使用的外部截图检索参考；没有公开 API，不自动上传本地画面。
- [tinyMediaManager](https://www.tinymediamanager.org/docs/movies/settings)：本地元数据、媒体技术信息复用与图片缓存的职责划分。
- [Jellyfin NFO](https://jellyfin.org/docs/general/server/metadata/nfo/) 与
  [影片目录规范](https://jellyfin.org/docs/general/server/media/movies/)：同名 sidecar、来源 ID 和字段锁的互操作。
- [beets](https://docs.beets.io/en/stable/reference/cli.html#import)：增量导入与保留既有元数据的流程取舍。
- [hls.js](https://github.com/video-dev/hls.js/blob/master/docs/API.md)：媒体绑定、错误处理与播放实例释放。
- [HTTP Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)：来源退避的协议依据。

## 明确取舍

- 不把番号作为数据库主键；无码、国产长尾、欧美场景和同片多号都能使用同一模型。
- 不把搜索第一条直接写入媒体库；确定性身份可自动接受，模糊候选必须审核。
- 不承诺未验证站点或私有 API 的覆盖率。麻豆、91 等内容主要依靠标题、URL、外部 ID 和用户确认；后续来源必须用离线 fixture 验证。
- Stash、ThePornDB、MetaTube 等属于可选联邦或连接器，不是核心数据库的前置条件。
- 片商全量目录抓取与单文件识别是不同任务；catalogue provider 使用独立查询模式。
- `.strm` 播放地址不是元数据详情页。扫描阶段不访问它，URI 凭据和查询参数也不进入持久化线索。

2026-10-27 的联网核对、独立实现与后续边界见 [参考项目功能审计](reference-audit.md#2026-10-27-整体复查与独立改进)。
