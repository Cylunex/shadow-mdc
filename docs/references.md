# 参考与取舍

Shadow MDC 的代码与数据模型从零实现。下列项目只用于确认常见工作流、部署形态和生态接口，不复制其源码或界面结构。

- [JavSP](https://github.com/Yuukiy/JavSP)、[MDCX](https://github.com/sqzw-x/mdcx)：番号扫描、NFO 与本地整理的基础场景。
- [OpenAver](https://github.com/slive777/OpenAver)：来源状态、人工浏览和本地桌面体验。
- [Javinizer Go](https://github.com/javinizer/javinizer-go)：CLI、Web、容器和预览式整理。
- [Stash](https://github.com/stashapp/stash) 与 [CommunityScrapers](https://github.com/stashapp/CommunityScrapers)：场景模型、指纹候选与站点适配器边界。
- [FSS](https://github.com/Anastylosis/FSS)：片商目录增量导入，适合作为独立 catalogue provider。

## 明确取舍

- 不把番号作为数据库主键；无码、国产长尾、欧美场景和同片多号都能使用同一模型。
- 不把搜索第一条直接写入媒体库；确定性身份可自动接受，模糊候选必须审核。
- 不承诺未验证站点或私有 API 的覆盖率。麻豆、91 等内容主要依靠标题、URL、外部 ID 和用户确认；后续来源必须用离线 fixture 验证。
- Stash、ThePornDB、MetaTube 等属于可选联邦或连接器，不是核心数据库的前置条件。
- 片商全量目录抓取与单文件识别是不同任务；catalogue provider 使用独立查询模式。
