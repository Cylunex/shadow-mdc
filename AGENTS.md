# Shadow MDC 开发约定

本项目从零实现，不复制 Amane、MDCX、JavSP 或其它参考项目源码。

## 定位

Shadow MDC 是本地优先的影片元数据识别、刮削和整理服务。JAV 是首要场景，同时支持国产长尾、欧美场景、无稳定编号及用户指定 URL 的内容。

## 技术栈

- Python 3.12+、FastAPI、SQLAlchemy、SQLite
- React、TypeScript、Vite
- Docker

## 命令

Windows 命令优先通过 Git Bash 执行。Python 环境使用 uv，前端使用 pnpm。

```bash
just setup
just test
just check
just dev
```

## 约束

- 所有 Python 公共函数、方法与数据结构必须有完整类型标注。
- 内部数据传递使用 dataclass 或 Pydantic model，不使用无结构 dict。
- 导入放在文件顶部，不使用反射绕过类型检查。
- 数据库身份不依赖番号；番号只是可选的 Identity。
- 远程来源只负责候选和字段快照，不可直接修改数据库或文件。
- 低置信候选不得自动写成正式作品。
- 文件移动、覆盖、链接和删除必须先生成可审计计划。
- 默认范围含元数据与公开图片；磁力/离线/STRM/Emby 深链已实现。在线外站与 VOD 片源仅从本地/NAS 的 `playback-sites.local.json` 加载（gitignore），真实站点 URL 不得提交远端；见 docs/playback.md。
- 新增来源必须包含离线 fixture、合法/非法输入和解析漂移测试。
