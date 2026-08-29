# Shadow MDC

本地优先的影片元数据识别、刮削与整理服务。主要覆盖 JAV，同时面向国产长尾、欧美场景及其他非标准编号影片。

## 设计重点

- 多身份识别：番号、标题、来源 URL、provider ID、oshash。
- 候选优先：模糊结果进入待确认，不让错误首条结果污染媒体库。
- 多源可追溯：字段、图片和外部 ID 保留来源快照。
- 安全整理：先生成操作计划，再显式执行。
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
