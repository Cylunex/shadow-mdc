# 冷启动与列表性能预算

提交: 见 `git log`。本文只记录几个已经踩过的坑和对应的不变量，避免下次改动把它们推翻。
下面的数字是在生产库副本（9157 works / 10278 work_actors / 2256 collections）上，用同一份
`data/` 对新旧代码各跑一遍 pipeline 得到的实测值（NAS：zfuse 挂载，stat≈60µs）。

| 路径 | 旧 | 新 |
|---|---|---|
| 启动：非 JAV 种子 upsert（2816 部） | 8.2 s | 0.01 s（未变化时跳过） |
| 启动：`sync_all_work_actors` | 4.28 s | 0.52 s |
| 启动：`repair_jav_actor_sources` | 2.36 s | 1.33 s |
| `/api/actors` 冷路径 | 6.56 s | 1.54 s |
| `/api/collections` 冷路径 | 1.23 s | 0.01 s |
| `/api/works/tags`、`/api/categories` | 0.26 s | 0.03 s |
| `/api/works` 卡片构建 | 1.43 s | 1.21 s |

## 1. SQLite 连接级 PRAGMA

`Database.__init__` 给 SQLite 引擎挂 `connect` 事件，每条池化连接都执行：

- `temp_store=MEMORY`：列表查询对整张 `works` 做 `IN (...)` + `ORDER BY` 时会用临时 B 树。
  一旦临时文件打不开，SQLite 报的是极具误导性的 `unable to open database file`（本项目在
  Windows 上就是这样整条 `/api/works` 500 的）。
- `cache_size=-16384`（16 MiB）、`mmap_size=256 MiB`、`synchronous=NORMAL`（WAL 下每次
  checkpoint 一次 fsync，而不是每次提交一次）。

`journal_mode=WAL` 写在数据库文件里只需设一次，其余都是**连接级**的，必须挂在 `connect` 事件上，
不能只在迁移连接里设。

## 2. 目录索引（`media/directory_index.py`）

列表页要为每个作品回答"`data/artwork/<id>` 里有 thumb/poster/fanart 吗"。逐作品
`glob` + `stat` 是每个请求上万次系统调用。现在每个目录只 `scandir` 一次并缓存 30 秒，
判断顺序是：

1. `data/artwork/<work_id>` 是否存在 → 一次 `is_dir`（新建的作品目录立刻可见）；
2. 目录里有哪些 stem → 一次目录快照；
3. 快照是本次调用里刚扫出来的就直接用，否则用一次 `is_file` 确认；不一致就 `refresh()` 重扫一次。

不变量：

- 不要用"目录列表快照"判断作品目录是否存在，新建目录会被缓存挡住，必须用 `is_dir`。
- 扫描失败的目录（不存在）不入缓存，否则新建目录要等 TTL。
- 不要用 `time.monotonic()` 差值判断"快照是不是这次刚扫的"：Windows 上它的分辨率是
  15.6 ms，同一次调用里的比较会误判。用 `directory_index.generation()`。
- 写图的代码路径写完后调 `directory_index.invalidate(<父目录>)`（`ArtworkStore`、
  `ensure_list_thumbnail`、`ActorImageCache._write` 都做了）。
- 列表接口传 `data_dir` 给 `_work_list_out` / `resolve_work_display_image`；不传时会退回逐条
  `Path.is_file()`，又是一次全量 stat。

## 3. 启动阶段只做"有变化"的活

冷启动依次执行：DB 迁移 → 旧本地目录修复 → 封面 adopt → JAV 演职员修复 → 演职员多对多同步
→ 非 JAV 种子导入。生产库 9k 作品时旧实现约 24 秒（NAS 上部署脚本给 90 秒窗口），现在：

- `repair_jav_actor_sources` 一次查出全部 JAV 来源快照，不再逐作品查询（原 9k 条语句）。
- `sync_all_work_actors` 预加载 `work_actors` 与 `actors` 名字索引，循环内零查询。
- `seed_non_jav_works_if_changed` 用 seed 文件 sha256 + `non-jav-seed` 身份条数做标记
  （`data/cache/<seed>-seed-state.json`）；两者都没变就整段跳过（原 2.8k 次 upsert / 8 秒）。
  改了种子文件、或库里的种子作品数变了，会自动重跑。

## 4. 列表与聚合接口的默认约定

- 聚合计数走一条 `GROUP BY`（`work_counts_by_collection`），不要在循环里对每个集合再查一次。
  `/api/collections` 原本是 2.2k 次 `list_works`（1.2 秒）。
- 只读标签的接口用 `repo.work_tags()`（单列）而不是 `list_works(lean=True)`。
- 静态索引（`data/javranking/honors-by-code.json`、`actor-honors.json`）按
  （路径, mtime, size）记忆化；一位演员读一次文件是错的（1667 位演员 = 1667 次 JSON 解析，
  4.4 秒）。
- `display_chips` / `canonicalize_tag` 有 `lru_cache`：它们在一个响应里会被调用上万次，且入参
  高度重复。
- 演员目录构建时不要用 `all(ref.id != work.id for ref in ...)` 去重，用 `set`（原本是 110 万次
  生成器迭代）。

## 5. 本地复现生产数据

NAS 的 `shared/data` 可以直接拉下来在本地跑（只读副本即可）：

```bash
python3 -c "import sqlite3;s=sqlite3.connect('file:shadow-mdc.db?mode=ro',uri=True);d=sqlite3.connect('/tmp/mdc/shadow-mdc.db');s.backup(d)"
# artwork/、actor-images/、category-covers/、javranking/ 一起带走，封面和榜单才有内容
SHADOW_MDC_DATA_DIR=/tmp/mdc SHADOW_MDC_DATABASE_URL=sqlite:////tmp/mdc/shadow-mdc.db \
  SHADOW_MDC_REDIS_URL= uvicorn --app-dir src shadow_mdc.api:app --port 8000
```

`SHADOW_MDC_REDIS_URL` 留空即可用进程内 TTL 缓存。第一次启动会写种子状态文件，之后启动只剩
DB 迁移与演职员同步，秒级完成。

## 6. 响应缓存的资源边界与故障回退

- 进程内缓存最多保存 256 条、32 MiB 的 UTF-8 键和响应正文；超大响应直接跳过缓存。
  写入时先清理过期项，再淘汰最久未读取的条目；替换、删除和清空均释放对应额度。
- Redis 连接与读写超时均为 0.5 秒。启动时不可达或运行中发生 Redis 错误时，当前进程切换到
  内存缓存并重试当前缓存操作；不会持续探测 Redis，重启进程后才重新选择后端。
- 故障日志只记录异常类型，避免连接异常正文泄漏凭据；数据库查询和业务写入仍按原流程执行。

回归测试：`uv run python -m pytest tests/test_response_cache.py`。
