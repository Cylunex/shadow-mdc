# Shadow MDC

本地优先的影片元数据识别、刮削与整理服务。主要覆盖 JAV，同时面向国产长尾、欧美场景及其他非标准编号影片。

## 设计重点

- 多身份识别：番号、标题、来源 URL、provider ID、oshash。
- 媒体入口：实体视频、`.strm` 引用、本地目录、映射盘与 UNC 网络共享。
- 无番号线索：结合文件名、目录上下文和用户可编辑的片商/系列/人物别名。
- 垃圾过滤：扫描前按可编辑词表过滤广告、试看、预告和推广媒体，且不删除源文件。
- 本地线索优化：优先文件名，再逐级回退目录名；无番号在线无结果后形成带分类的本地作品。
- 自动分类：逐文件按番号、目录关键词与中日韩/英文脚本识别 JAV、国产、韩国、欧美或其他内容。
- 双分支入库：有番号必须在线精确匹配，失败留待确认；无番号先搜索，未命中再接受本地优化记录。
- 演员作品库：Actor 与 Work 通过多对多关系关联，展示演员代表图和关联作品海报，并反向辅助后续目录识别。
- 非 JAV 演员管理：内置名单可在演员库中新增、编辑、删除和上传头像；数字分段文件可一次确认演员并应用到整个目录。
- 作品/文件分层：标题、演职员和图片稳定归属作品；分辨率、编码、码率、HDR 与音轨按实际文件独立更新。
- 作品图片仓库：图片按作品 ID 集中保存，重新扫描不重复下载，整理时才复制为 poster/fanart。
- 非 JAV 严格整理：必须先确认目录演员；实体视频还要从文件生成并绑定本地截图，`.strm` 只校验远程地址并免截图。
- 多源可追溯：字段、图片和外部 ID 保留来源快照。
- 中文标题：新识别作品自动翻译，已有作品可批量补翻译；原文和来源快照永久保留并使用独立缓存。
- 五种输出：原目录或独立作品库写 `movie.nfo` 和可用图片，并支持复制、移动、硬链接和软链接。
- 安全整理：批量计划带摘要令牌，目标或配置变化后旧计划自动失效。
- 可替换来源：普通 JAV 并发查询 R18.dev、FANZA、JavLibrary、MGStage、JavDB、JavBus、Jav321、
  AirAV、AVSOX 和 FreeJavBT 元数据；FC2 额外路由 FC2Club、FC2Hub、FC2 Contents Market、Paipancon 与 FreeJavBT，并保留
  ThePornDB/Stash-box 和通用 JSON-LD URL。
- 元数据边界：不提供影片下载、磁力或在线播放。

## 快速开始

API 响应缓存（可选 Redis）：设置 `SHADOW_MDC_REDIS_URL`（本地默认 `redis://127.0.0.1:6379/0`，NAS 生产 `redis://192.168.0.21:6379/0`）。未配置或 Redis 不可达时自动回落到进程内 TTL 字典。年度 TOP / JavRanking 等静态列表长 TTL，种子写入后失效。

JavDB 年榜 TOP250（2008→今）为本地优先：在 box 从 jinjier SQLite 导出到 `data/javranking/yearly/`，再 rsync 到 NAS；历史年份永不远程重拉，仅当前公历年可通过 `SHADOW_MDC_PROXY_URL` 可选刷新。

```bash
just setup
just dev
```

API 默认监听 `http://127.0.0.1:8000`，Web 开发服务器监听 `http://127.0.0.1:5173`。


## NAS：deploy ops 备份

`scripts/nas/deploy-mdc.sh` 成功部署后，在 `/data/project/.ops-backups/shadow-mdc/` 下**最多保留 2 个**历史 `pre-<commit>-<stamp>/` 备份（`SHADOW_MDC_BACKUP_KEEP`，默认 2），多余的立即清理。详见 [docs/user-guide.md](docs/user-guide.md#nasdeploy-与-ops-备份保留)。

## NAS：每周 r18.dev dump

上游 dump 多在周二更新。NAS 上用 in-repo 任务（**不是** Grok Bot）在周三 10:01（`Asia/Shanghai`）执行：

1. 记录 JAV 缺口统计 → `supervisorctl stop shadow-mdc`
2. `import_r18_dump.py --refresh`（`SHADOW_MDC_PROXY_URL`，默认 `http://192.168.0.110:7893`）
3. dump 有更新时 `enrich_from_r18_dump.py`（**只更新已有作品，绝不新建**）
4. `supervisorctl start` → `/api/health`（冷启动最多约 90s）→ 复核统计（`jav_total` 不得增加）

数据在 `shared/data/r18-dumps/`（deploy 会保留，勿提交 git）。默认关闭：设
`SHADOW_MDC_R18_DUMP_REFRESH_ENABLED=true` 后再加 crontab。详见
[docs/user-guide.md](docs/user-guide.md#nas-每周-r18dev-dump-刷新)。

## NFO 手动字段锁

扫描时读取作品目录/STRM 旁的 `movie.nfo` 与 poster/fanart。NFO 中带
`<shadowmdc_locks>title,plot</shadowmdc_locks>`、Emby `<lockedfields>Name|Overview|Cast</lockedfields>`
或 `<lockdata>true</lockdata>` 的字段视为手动编辑：值写回作品并加锁，之后多源/强制重刮只补空字段或未锁字段。
应用内编辑的锁优先于 NFO；空值永不覆盖（演员防清空栅栏不变）。导出 NFO 时会写回这两个锁标记。

## SubtitleCat 中文字幕（可选）

`SHADOW_MDC_SUBTITLECAT_ENABLED=true` 后，115/OpenList STRM 导出完成时按番号从 subtitlecat.com
取简中（无则繁中）字幕，写为 `<名称>.zh.srt`（Emby 识别）。已有字幕跳过；失败只记日志，不影响刮削/导出。
可调：`SHADOW_MDC_SUBTITLECAT_BASE_URL`、`SHADOW_MDC_SUBTITLECAT_MIN_INTERVAL_SECONDS`（默认 3）、
`SHADOW_MDC_SUBTITLECAT_MISS_TTL_HOURS`（未命中缓存，默认 72，存 `data/subtitlecat-misses.json`）；走 `SHADOW_MDC_PROXY_URL`。
只读查询：`GET /api/subtitles/subtitlecat?code=SSIS-001`（不写文件）。

## 项目状态

架构和验收范围见 [docs/architecture.md](docs/architecture.md)，安装、Docker 与媒体库使用见
[docs/user-guide.md](docs/user-guide.md)。参考项目与设计取舍见
[docs/references.md](docs/references.md)。

本机参考项目的逐项功能审计与吸收状态见
[docs/reference-audit.md](docs/reference-audit.md)。

## Resumable jobs, subtitle backfill and organize undo

- **Interrupted tasks**: on startup, any `task_runs` row left unfinished is closed as `interrupted`
  (`summary.resumable` tells whether it can continue). `POST /api/tasks/{id}/resume` re-runs a
  `scan` (with `only_new`), `identify:library` (`skip_identified` + `continue_failed`) or
  `subtitles:backfill` from its checkpoint.
- **Subtitle backfill** (needs `SHADOW_MDC_SUBTITLECAT_ENABLED=true`): walks every directory
  holding `.strm` files under the STRM root, skips directories that already have any subtitle, and
  fetches `<stem>.zh.srt` from SubtitleCat at the fetcher's rate limit
  (`SHADOW_MDC_SUBTITLECAT_MIN_INTERVAL_SECONDS`, default 3s per request).
  - `POST /api/subtitles/backfill` `{"root"?, "limit"?, "dry_run"?, "restart"?}` start/resume
    (`limit` caps remote lookups per run).
  - `GET /api/subtitles/backfill` progress; `POST /api/subtitles/backfill/stop` pause.
  - Progress is checkpointed after every directory (`job_checkpoints` table); a backfill that
    was running at shutdown resumes automatically on startup. Ten consecutive errors pause it.
- **Organize undo**: `organize/apply` records a reverse-operation journal (moves, created
  files/links, overwritten NFO text, removed dirs; filtered files go to
  `data/organize-trash/<task>` instead of being deleted).
  - `GET /api/organize/runs/{task_id}/journal` shows it; `POST /api/organize/runs/{task_id}/undo`
    with `{"dry_run": true}` (default) previews, `{"dry_run": false}` rolls back.
  - Undo never overwrites: steps whose files changed or whose original slot is occupied are
    skipped and reported.
