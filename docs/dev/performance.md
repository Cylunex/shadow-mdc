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

- 不要用跨请求缓存的目录快照判断作品目录是否存在，新建目录会被 TTL 挡住。单条解析用 `is_dir`；
  批量列表用本次请求刚读取的根目录快照，下一次请求重新读取，不能复用旧快照。
- 扫描失败的目录（不存在）不入缓存，否则新建目录要等 TTL。
- 不要用 `time.monotonic()` 差值判断"快照是不是这次刚扫的"：Windows 上它的分辨率是
  15.6 ms，同一次调用里的比较会误判。用 `directory_index.generation()`。
- 写图的代码路径写完后调 `directory_index.invalidate(<父目录>)`（`ArtworkStore`、
  `ensure_list_thumbnail`、`ActorImageCache._write` 都做了）。
- 列表接口传 `data_dir` 给 `_work_list_out` / `resolve_work_display_image`；不传时会退回逐条
  `Path.is_file()`，又是一次全量 stat。
- 整页解析使用 `resolve_work_display_images`，先枚举一次作品图片根目录，再按作品 ID 去重。
  `_work_list_out` 接收批量生成的 `image_urls`，避免同一作品跨演员或推荐区重复解析。

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

## 7. 页面冷加载（2026-10-27）

这轮优化以响应缓存为空为前提。脚本创建临时 SQLite 与合成资料：5000 部作品、250 位演员、
15000 条演职员关系，包含较长剧情、评论与图片信息；非 JAV 演员目录也有数据。每个 API 测量
三次，**每次请求前清空响应缓存和目录索引**。下表是同一台本机上，对改动前提交 `9f6c9f6`
与当前实现顺序独立执行的中位数；没有同时运行测试。操作系统文件缓存未清空，数字不代表 NAS
网络盘或真实生产页面的端到端耗时。

| 路径 | 改动前 | 改动后 |
|---|---:|---:|
| 影片页列表（新前端启用 compact） | 287.1 ms | 224.1 ms |
| 演员目录 | 415.4 ms | 238.1 ms |
| 非 JAV 演员目录 | 105.0 ms | 21.4 ms |
| 作品相关推荐 | 103.5 ms | 77.2 ms |

影片列表解压后的 JSON 从 4,316,121 字节减为 2,786,121 字节（约 35%）；旧客户端不传
`compact` 时仍返回原有格式，前端 schema 与 Python WorkOut 都能还原省略的默认值。
两种格式的响应缓存独立，失效规则一致。这里的字节数不等于 gzip 后的线上传输量。

实现约束：

- 前端首次偏好设置返回不再触发第二次全库下载；同一列表请求进行中，切页复用正在执行的请求。
  想看状态在本地叠加，修改偏好无需重拉片库。操作后的相互独立刷新并行执行。
- 影片详情不预取整个片库；详情主体独立完成显示，相关推荐和离线任务随后补齐。
  切换作品取消旧请求，迟到响应不能覆盖新作品。
- 演员 GET 不再序列化和写回完整知识目录，只解析卡片预览所需的图片；总作品数和最多 80 个
  搜索番号仍基于完整作品关系。关系构建复用单个作品引用和演员累积器，JAV 判断在本次构建内去重。
- 非 JAV 目录先在 SQL 排除 JAV，并只读轻量字段；空演员目录直接返回，不扫描全库。
  相关推荐也使用轻量查询，避免解码全库剧情、评论和字段来源。
- 榜单分区导航默认只读本地索引，不依次拉取全部远程榜单。已落盘榜单优先展示，远程刷新通过
  页面已有的刷新入口执行；未落盘时仅获取当前选中的榜单。入库状态使用批量番号和本地文件查询。
- 数据变更会失效榜单列表中的入库状态；分区条目在当前榜单读取后同步更新。搜索不依赖缓存写入
  成功，超大响应跳过缓存或缓存关闭时仍能返回结果。

复现：安装开发依赖后执行：

```bash
.venv/bin/python scripts/benchmark_cold_pages.py --works 5000 --repeats 3
```

回归入口：`tests/test_cold_page_loading.py`、`web/src/App.loading.test.tsx` 和
`web/src/hooks/useWorkPage.test.tsx`。测试覆盖精简格式还原、旧格式隔离、请求去重、主体先显示、
旧响应取消、榜单导航不联网、批量查询和新图片即时可见。

本轮验证：Python 全量 767 项、前端回归 29 项通过；前端类型检查和生产构建通过。
图片解析、演员目录、新增基准脚本与回归测试通过 Ruff 和严格 mypy；API/Repository 的其它历史
静态检查问题未批量修改。

## 8. 共享查询与冷加载等待链（2026-10-27）

参考 Peach `1b8beb9b` 的 `frontend/src/query/client.ts`、`media-sources.ts` 与
`docs/FRONTEND.md` 查询契约，使用 TanStack Query v5 独立接入 MDC。共享的是查询资源与进行中的
请求，不复制参考项目代码。`web/src/query/client.ts` 是唯一 QueryClient；`resources.ts` 集中定义
查询键、取数函数与新鲜期。页面、组件与主动读取必须复用这些定义，不能给同一资源另起查询键。

### 缓存为空时也减少工作

以下为对照 `1c79ea8` 的首批 JSON 数据调用预算，按正常生产挂载、查询缓存为空计算；
不含 JavaScript、图片、后续播放解析与定时刷新。回归用例清空 QueryClient，并通过请求计数与
可控的未完成 Promise 验证，而非靠延长 TTL 获得命中。

| 页面 | 原首批数据调用 | 现在 | 冷路径变化 |
|---|---:|---:|---|
| 影片 | 4 | 2 | 仅片库与偏好；不读取任务历史、媒体库配置 |
| 演员 | 5 | 3 | 两份目录独立显示，不再等待 `Promise.all` 的最慢项 |
| 分类 | 4 | 2 | 分类与偏好；不读取无关的全局核心数据 |
| 作品详情 | 6 | 2 | 主体与偏好先取；主体可显示后再启动推荐与离线状态，合计仍需 4 次 |
| 外站刷流 | 5 | 2 | 偏好与当前 feed；切到本库模式才读取片库 |

设置页的账户状态、设置、离线任务与流水线各自完成各自显示，慢任务不再阻塞账户信息。
任务中心的离线列表按独立 5 秒节律更新，不因每帧 SSE 快照重新拉取。后台完成快照以任务 ID
和完成时间比较，失效媒体库与偏好；比较使用 Map，避免历史任务较多时逐项线性查找。

### 资源与一致性

- 影片、演员、分类、详情、推荐与演员完整作品默认新鲜期 30 秒；偏好和离线列表 5 秒；
  任务历史、账户状态、流水线新鲜期 0，主动读取仍可合并同一时刻的请求。
- 组件挂载时直接显示已有数据；过期时后台更新。没有预先等待数据的路由层，因此保留
  TanStack 的挂载重取规则，不沿用 Peach 针对“路由先预取”的 `refetchOnMount: false`。
- 不自动重试、不因窗口焦点或浏览器重连产生额外刷新；本机 API 使用 `networkMode: always`。
  失败不伪装为空数据，也不作为成功缓存；页面保留错误与手动刷新入口。
- React 观察者与 `queryClient.fetchQuery(resources.xxx())` 共用进行中的请求。查询取消由客户端
  统一管理：仍有观察者时，某个组件卸载不会中止共享读取；最后一个观察者离开时可取消。
  普通已完成缓存离开页面后保留，过期再读时后台更新；不把查询结果复制到持久浏览器存储。
- 元数据写入先取消旧读取，再按资源组失效；后台更新只重取活跃资源，隐藏页面下一次进入时取新值。
  偏好写入直接发布经 schema 校验的完整返回值，取消旧偏好读取，不重新下载全库。
  “想看”在影片与详情叠加同一份偏好；没有加载过的片库不能因局部修改被建立为空缓存。
- 播放解析、来源诊断、整理预览虽然部分采用 POST，但不是媒体库写入，不能因它们失效整库。
  真正的写入保持各自执行，不参与读取请求合并。
- 显式刷新同时重取当前活跃查询，保留页面已有内容。客户端失效不会自行清空服务端响应缓存；
  服务端仍沿用现有 TTL 与业务写入失效规则。
- 无观察者且未在请求中的资源最多保留 96 个，并在闲置 5 分钟后回收。活动资源和进行中请求不
  参与数量淘汰；这是每个页面会话内的缓存，不提供跨标签页或跨服务进程的请求合并。

### 首次代码下载

设置、榜单和订阅独立为按需页面块，其余主要页面保持直接可用。引入查询库后的单入口曾为
526.96 kB / gzip 153.32 kB；拆分后三个低频页面按需加载，影片页入口约 478.7 kB / gzip 140.5 kB，
低于上一版的 488.79 kB / gzip 142.22 kB。尺寸由本机生产构建得到，不等于线上加载耗时。
部署时需要保留完整 dist 目录中的入口和页面块，不能只复制入口脚本。本次仅验证构建，不执行部署。

回归入口：`web/src/query/resources.test.tsx`（真实 API 包装与模拟 fetch）、`App.loading.test.tsx`、
`hooks/useWorkPage.test.tsx`、`views/SettingsView.loading.test.tsx`、`components/TaskCenter.queries.test.tsx`。
覆盖跨读者合并、共享取消、迟到响应、失效与偏好快照、失败和 schema 校验、过期后台更新、容量上限，
以及冷启动预算、慢支路隔离、按需片库和事件快照不放大请求。

尚未在真实 NAS 网络与浏览器下记录本轮端到端首屏时间；本节只报告可重复的请求预算、行为验证和构建尺寸。

验证：Python 全量 770 项、前端 61 项通过；前端 TypeScript 检查与生产构建通过。
