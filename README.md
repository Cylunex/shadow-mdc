# Shadow MDC

本地优先的影片元数据识别、刮削与整理服务。主要覆盖 JAV，同时面向国产长尾、欧美场景及其他非标准编号影片。

## 设计重点

- 多身份识别：番号、标题、来源 URL、provider ID、oshash。
- 媒体入口：实体视频、`.strm` 引用、本地目录、映射盘与 UNC 网络共享。
- 无番号线索：结合文件名、目录上下文和用户可编辑的片商/系列/人物别名。
- 垃圾过滤：扫描前按可编辑词表过滤广告、试看、预告和推广媒体，且不删除源文件。
- 本地自动建档：优先文件名，再逐级回退目录名；通用分集名生成稳定且可读的独立作品名。
- 主分类：媒体库显式归入 Japan、China、Korea、Europe 或 Other，与在线来源路由分离。
- 候选优先：模糊结果进入待确认，不让错误首条结果污染媒体库。
- 多源可追溯：字段、图片和外部 ID 保留来源快照。
- 五种输出：原目录写同名 NFO，或预览后复制、移动、硬链接、软链接到独立作品库。
- 安全整理：批量计划带摘要令牌，目标或配置变化后旧计划自动失效。
- 可替换来源：内置网页来源、ThePornDB/Stash-box 与通用 JSON-LD URL。
- 元数据边界：不提供影片下载、磁力或在线播放。

## 快速开始

```bash
just setup
just dev
```

API 默认监听 `http://127.0.0.1:8000`，Web 开发服务器监听 `http://127.0.0.1:5173`。

## 项目状态

架构和验收范围见 [docs/architecture.md](docs/architecture.md)，安装、Docker 与媒体库使用见
[docs/user-guide.md](docs/user-guide.md)。参考项目与设计取舍见
[docs/references.md](docs/references.md)。

本机参考项目的逐项功能审计与吸收状态见
[docs/reference-audit.md](docs/reference-audit.md)。
