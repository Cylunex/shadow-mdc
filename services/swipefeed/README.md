# shadow-mdc swipefeed sidecar

Vendored tube listing / resolve / media-proxy service used by Shadow MDC under `/api/swipe/*`.
Browser API prefix: set `SWIPEFEED_BROWSER_API_PREFIX=/api/swipe`. Site toggles live in `SWIPEFEED_DATA_DIR` (never git).

---

# swipefeed-web

Web prototype of Swipefeed — server-side listing + resolve for public tube sites, full-screen vertical swipe player.


## UX (2026-10)

- Directional shallow media preload (hidden warm `<video>` / short-lived HLS, `maxBufferLength≈4`)
- Plugin circuit breaker + latency-weighted mix; settings show health colors / auto-break; re-enable clears circuit
- Resolve/play failure auto-skips to next; lightweight TTFF hint
- Bandwidth save: `capLevelToPlayerSize`, lower `startLevel`; `saveData`/slow `effectiveType` shrinks preload
- Local favorites + history (`localStorage` only)
- Mute (`M`), Media Session next/prev, optional PiP
- Gesture zones: left=prev / right=next / center=pause; `overscroll-behavior-y: contain`; `prefers-reduced-motion`

## Stack

- Node 20 + Express + undici (optional HTTP(S) proxy)
- Static dark Chinese UI (`public/`)
- Plugins under `src/plugins/`

## 1.8.0：列表卡片元数据 / 列表缓存 / 分类表合一

### 卡片元数据（`src/util/card-parse.js`）

`collectHrefCards` 原来只按 href 抠 id，卡片一律 `title: 'Video <id>'`、`cover: null`。
现在以命中 href 为中心取「所在卡片块」（到下一个 `<a` 之前，上限 4 KB，块尾再取 320 字符供作者回落），
在块内解析：

| 字段 | 优先级 |
|---|---|
| `title` | 显式 `titleGroup` → `<a title>` / `aria-label` → `<img alt>` → 标题标签 → 锚文本 |
| `cover` | `data-src / data-original / data-lazy-src / data-thumb / data-cover / data-poster` → `srcset` → `src` → `<source>` → `<video poster>` → CSS `background-image`（跳过 `data:`、`blank.gif`、`spacer`、`1x1`） |
| `duration` | `data-duration / data-dur / data-length / data-time / duration / content` → 文字 `12:34` → 归一化 `M:SS` / `H:MM:SS`（支持 `PT12M34S`、纯秒数、`1小时2分3秒`） |
| `author` | `data-author / data-model / data-uploader` → `/models/`、`/pornstars/`、`/channels/` 链接 slug |

同一套算法已按 1:1 移植到 LumenBox 的 `TubeCardParser.kt`，两端夹具一一对应。

### 列表页缓存（`src/util/list-cache.js`）

TTL 60s + **单飞**（同 URL 并发只打一次上游）+ LRU 上限 200，失败不缓存；`factory.listFrom` 已接上。

- `SWIPEFEED_NO_CACHE=1` 全关（探测脚本排障用）
- `SWIPEFEED_CACHE_TTL_MS=<ms>` 调 TTL

### 分类模板只有一张表

`CATEGORY_TEMPLATES = { ...CATEGORY_URL_PATTERNS }`。之前两张表各写一份且形状对不上
（`eporner` 一处 `/cat/` 一处 `/category/`、`3movs` 的 key 写成 `threemovs`、
`redtube`/`perfectgirls`/`hellporno`/`porndoe` 与插件实现不一致），现在按插件实际拼出的形状统一。

### 自检

```bash
npm run test:cards    # 卡片元数据夹具（28 组断言，离线）
npm run test:cache    # 列表缓存 TTL/单飞/LRU（7 组断言，离线）
npm test              # 上面两个一起跑
node ../LumenBox/android/scripts/check-site-parity.mjs   # 与 App 端站点对表（49/49）
```

## API

- `GET /health` — proxy + per-site circuit/latency snapshot
- `POST /api/circuit-breaker/reset` `{site}` or `{all:true}`
- `GET /api/sites` — enabled sites only (`?all=1` includes disabled)
- `GET/POST /api/site-settings` — enable/disable map (`data/site-settings.json`)
- `GET /api/feed?mode=single|random|category&site=&category=&cursor=`
- `GET /api/search?q=&cursor=&site=`
- `POST /api/resolve` `{site,id,pageUrl}` — 返回的 `url`/`qualities[].url` 已换成本地代理地址，上游原址在 `directUrl`
- `GET /api/png-hls?u=&r=` — unwrap PNG-disguised HLS (rou.video)
- `GET /api/hls?u=&r=` — HLS 代理：清单就地改写（分片/子清单/`EXT-X-KEY`/`EXT-X-MAP`/`EXT-X-MEDIA`），分片与密钥原样转发
- `GET /api/stream?u=&r=` — 直接 MP4/TS 代理，透传 `Range`（可拖动），流式转发不落盘

## 播放链路：为什么要本地媒体代理

浏览器直接拉跨域媒体会死在三种情况，而服务端取数时能带对 `Referer`/`UA`，所以统一把媒体拉回本地再喂播放器：

| 现象 | 站点例子 | 原因 |
|---|---|---|
| `ERR_BLOCKED_BY_ORB` | 3GO（media.3go.fun 原画） | 跨域 + MIME 不明确，Chrome 直接拦 |
| `ERR_FAILED` / 403 | phncdn 分片、hdcdn、porntrex | CDN 校验 `Referer` / 会话，播放器发不出来 |
| `CONTENT_LENGTH_MISMATCH` | porntrex 等 | 上游带 `Content-Encoding`，`Content-Length` 是压缩后长度 |

实现要点（`src/routes/media.js`）：

- `proxyUrl()` 在 `/api/resolve` 里统一改写，`directUrl` 保留上游地址供「复制直链」
- 只对“拿到响应头”计时（30s），开始吐字节后不再 abort —— 否则长视频会在 30s 处被切断并触发未处理 error 事件把进程带崩（实测踩过）
- `pipeline()` 接错误 + `req.on('close')` 销毁上游流：拖动进度条导致的客户端中断不再变成未捕获异常
- 媒体请求带 `Accept-Encoding: identity`；上游有 `Content-Encoding` 时不再转发 `Content-Length`
- 前端：经代理的 HLS 一律走 hls.js（同源，无跨域问题；AES-128 清单原生播放器会静默失败）
- `index.html` 里 `preload` hls.js：避免被一屏几十张封面请求挤到队尾（浏览器每主机 6 条连接）

### 封面（`/api/img`）不能拖住播放

死图（CDN 522/超时）会让一屏 20+ 张封面各占一条连接最多 12 秒，连 hls.js 都加载不出来 —— 表现为“点了没反应”。
现在：4 秒放弃 + 90 秒负缓存 + 直接 `204`，封面地址还会带 `&r=<详情页>` 作 Referer（防盗链站点才给图）。

### 仍然放不出来的站（上游限制，不是链路问题）

| 站点 | 现象 | 原因 |
|---|---|---|
| xhamster | 代理 403 | MP4 带 `key=...` 令牌，CDN 对本机 IP/会话拒绝（带不带 Referer 都 403） |
| eporner | 代理 403 | 同上，`gvideo.eporner.com` 直接拒绝 |
| netflav | 代理 403 | 走 `cc3001.dmm.co.jp`，区域限制 |
| 海角（haijiao） | `fragParsingError` | 预览清单是 AES-128，但分片用清单里那把 key 解不开（需要海角自己的密钥合成，LumenBox 的 `HaijiaoKeys` 就是干这个的） |
| hqporner | `No playable stream` | 解析器还没覆盖它当前的播放页结构 |


## Aggregators (this batch)

| id | displayName | notes |
|----|-------------|-------|
| rou | 肉視頻 | list + search; HLS via png-hls proxy |
| x134 | 134X長視頻 | popular/featured SSR; Twitter MP4 |
| bilifun | Bili影院 | JAV/AV 中字; page m3u8 |
| netflav | Netflav | discovery; streams often empty |
| hsex | 好色TV | list/search; CDN HLS |
| aggrcine | 聚合影视 | av.telstra.com.cv spider API |
| pektino | Pektino (X 排行) | 来自 `tool\` 的「视频站增强」脚本；列表走 JSON-LD + `/movie/<id>`，解析取该作品自身的 video.twimg.com MP4 |
| threego | 3GO 国产自拍 | 来自 `tool\` 的六合一脚本模块 1；MediaCMS 实例，`/api/v1/media` 出列表、详情给原画 MP4 |
| haijiao | 海角社区 | 来自 `tool\` 的「m3u8提取」脚本 + LumenBox 实现；`/api/topic/*` 出列表/分类（响应 data 多层 base64），播放走列表/详情里的公开预览 |
| videoshub | VideosHub 视频库 | 整合 GitHub `moluhualuo/-VIP-`（VideosHub 下载器）；自带 protobuf 信封接口 + X-Sign 请求签名 + HMAC 媒体签名清单 + AES-128-CBC 分片解密，见下 |

## VideosHub 整合（GitHub moluhualuo/-VIP-）

上游是个 Python + tkinter 的桌面下载器。整合方式是按原逻辑在 Node 里重写一份，而不是调它的 exe：

| 上游（Python） | 本项目（Node） | 位置 |
|---|---|---|
| 手写 protobuf 信封 `{1:code,2:msg,3:data}` | `vint/readVarint/fields/fieldStr` | `src/plugins/videoshub.js` |
| `api_sign()`：`sha256(secret\nMETHOD\n/path\nquery\nts\ndev)` | `apiSign()` | 同上 |
| `media_sign()`：`hmac_sha256("full\|vid\|line\|quality\|index.m3u8\|ts\|nonce")[:16]` | `mediaSign()` | 同上 |
| `Client.login()` 设备登录（`POST /api/v1/auth/device`） | `VideosHubClient.login()`，会话在进程内复用，过期自动重登 | 同上 |
| `decode_list()`（`/api/v1/video/list?page=&page_size=`） | `decodePage()` → 卡片 `{id,title,cover,duration}` | 同上 |
| `fetch_preview_url()` + `plan_playlist()` | `previewUrl()` + `signFullPlaylist()` | 同上 |
| `parse_playlist()`（`#EXT-X-KEY` / IV / MEDIA-SEQUENCE） | `parsePlaylist()` | 同上 |
| `download_and_merge()`（并发取分片、按 index 顺序落盘、AES-128-CBC） | `downloadAndMerge()`（`node:crypto` 解密） | 同上 |
| `remux_ts_to_mp4()`（ffmpeg `-c copy` → PyAV 兜底） | `remux()`（ffmpeg `-c copy`；没有 ffmpeg 就保留 `.ts`） | 同上 |
| tkinter GUI | 查看页卡片按钮 + `/api/download` | `public/views.js`、`src/routes/download.js` |

用法：

- 查看页选站点 **VideosHub 视频库** → 卡片墙（30 条/页，封面走 `/api/img` 代理）
- 卡片 **详情** = 签名后的完整清单地址（交给浏览器 hls.js 播，清单带 `ACAO:*`，key 与分片都能取）
- 卡片 **下载** = `GET /api/download?site=videoshub&id=<id>&quality=720p[&max=50][&workers=8][&mp4=1]`，并发取分片 → 解密 → 顺序拼 `.ts` →（有 ffmpeg）转 MP4，完成后作为附件回传
- `GET /api/download/status?site=videoshub&id=<id>` 只探清单：分片数 / 时长 / 加密方式 / 是否有 ffmpeg
- `GET /api/download/files[?clean=1]` 看/清服务端拼接目录

边界（与上游一致）：`480p` 服务端直接 403，所以默认 `720p`；接口没有搜索，查看页的搜索是**拉几页后本地过滤**；本机若没装 `ffmpeg`，`/api/download` 回传的就是可直接播放的 `.ts`（装上 ffmpeg 即自动出 MP4）。

## 查看页（按站点展开视频列表）

前端顶栏 **查看**（或 `#/browse/<site>`）打开查看面板，参照 LumenBox 的 Browse 页：

- **左侧站点抽屉**：按 可直接查看 / 需浏览器会话 / 其它站内源 分组，带搜索框（工具栏搜索框与 `/` 键都能聚焦，回车直开第一个结果）；宽屏默认展开，窄屏收起后用工具栏「☰ 站点」拉出，`Esc` 先收抽屉再关面板
- **分类是动态解析的**（不再是写死的 6 个词），见下节
- 网格 / 列表两种布局（`g` 键或卡片「列表」按钮切换，选择记忆在 localStorage）
- 卡片操作：点卡片即播；「详情」按需解析并展开直链/画质（可复制）、「下载」（有服务端下载能力的源）、「源站」
- 触底自动加载下一页（服务端按 `site:id` 去重，站点忽略分页参数时自动停止续页）
- 点卡片进全屏播放复用同一套解析 / 预热 / 熔断 / 历史，播放页滚到底会沿查看页的 cursor 继续拉同一站点
- 封面统一走 `/api/img` 代理（防盗链 + 只放行公网 http(s)，拒环回/私网/链路本地，单张 ≤512KB）
- **抓封面(慢)**：列表不带封面的源（pektino / 海角）默认打开，只对首屏少量条目补详情字段；海角的图片附件是要去掉 `.txt` 伪装后缀的真图
- `#/tools`、`#/tools/<id>` 打开**工具目录**页签：`tool\` 素材区条目 + 站点对照表 + 脚本副本下载

## 分类：每个站点动态解析

以前 `/api/browse/:id` 对没有自带分类的源回落到写死的 6 个词（asian/japanese/…），对多数站点其实点了就 404。现在是真去找，按可靠性从高到低：

| 顺序 | 来源 | 说明 | `categorySource` |
|---|---|---|---|
| 1 | 站点接口 | 插件自带解析：海角 125 个节点、MediaCMS `/api/v1/categories`、VideosHub | `plugin-api` |
| 2 | 站点页面 | 抓首页 / 分类索引页里的分类链接（连 `/c/`、`/channels/`、`/tags/` 都认），并用第一个样本反推该站 URL 形状；支持带前缀段的地址（`/category/58/voyeur`） | `homepage-links` |
| 3 | 目录页 | 只有分类链接、没有视频卡片的索引页（如 `/categories/`），同样抽出分类再校验 | `category-index` |
| 4 | 浏览器渲染 | 上面都拿不到时（分类是前端 JS 渲染的站，如 txxx），用本机 **headless Chrome** 渲染一次索引页再抽链接（`SWIPEFEED_BROWSER_FETCH=0` 可关闭） | `browser-render` |
| 5 | 路径探测 | 用候选 slug（asian/teen/milf/… 22 个）× 该站 URL 形状逐个请求，判定“是否真是列表页”后留下通过的 | `slug-probe` |

要点：

- **形状一致**：每个源通过 `categoryPattern` / `categoryTemplate` 暴露自己的分类 URL 形状（如 xhamster `/categories/{slug}`、eporner `/cat/{slug}/`），探测与过滤都读同一个来源 —— 所以“探测出来的分类一定是能出列表的”，不会再出现点了 404 的固定标签
- **任意 slug 都能过滤**：过滤时如果插件只认写死映射表，会按登记的 URL 形状 + 该源自己的解析函数再取一次（`category-template`）
- 有站点用 404 状态码返回可用分类页（drtuber、anyporn），所以判据是**有没有解析出卡片**，不是 HTTP 状态
- 探测有限额：单请求 6s 超时、单站总预算 25s、最多 30 次候选请求、连续 8 次失败即收手
- 结果缓存 30 分钟；分类条最左边标着来源与数量，还有个 **↻ 重探** 按钮随时重解析；分类 chip 的悬停提示就是该分类实际会请求的 URL
- 查全部站点：`GET /api/categories[?site=x&refresh=1]`，返回每站 `source / pattern / count / categories`
- 完全找不到分类入口的站点（spankbang / beeg / txxx …）就**不显示分类条**，而不是给一堆失效标签

实测（本机，2026-10-16）：50 个源里 **34+ 个**动态解析出分类 —— 海角 125 个节点、xhamster 60、thisvid 60、drtuber 60、hqporner 60、porntrex 60、zbporn 60、bigfuck 60、tnaflix 60、xvideos 38、perfectgirls 35、sunporno 30、3movs 24、youporn 21、anyporn 18、anysex 17、porndoe 19、pornhub 13、eporner/xnxx/porntube/hellporno/superporn/pornone 各 12~13、porngo 11、sexvid/rule34video 10、txxx 10（headless 渲染）…… 逐站抓第一个分类做端到端取数，多数能出真实列表。

剩下仍无分类的站点分三类，都不是“换个爬法”能解决的：

| 类型 | 站点 | 原因 |
|---|---|---|
| Cloudflare / 反爬挑战 | spankbang、4tube | 直接返回 403 挑战页，headless 也一样 |
| 前端空壳 / 需要交互 | beeg、empflix、tube8、redgifs | 分类列表要 JS 或点击才出现，渲染一次也拿不到（tube8 `/categories.html` 连渲染后也没有分类锚点） |
| 本来就不靠分类 | hsex、rou、x134、bilifun、netflav、pektino、aggrcine、videoshub、threego | 这些站没有分类概念，列表按热度/时间给；其中 pektino 有 20 个 `/category/*`，但那是客户端渲染，服务端取不到条目 |

“需浏览器会话”的站点（hstv / caoliu / xasian / 91·SP / javhub / 老王 / 春满院 / 艾薇社区 / 好妻网 / 套路影视 / 足控社 / 含羞草 / 镜像站 / 海角帖子页）与上面不同：它们需要**你自己的登录态**（localStorage 里的 uuid / cookie），服务端拿不到，所以在查看页只出索引，实际解锁仍由浏览器里的脚本完成。

### 这类站点在查看页长什么样

选中它们不是空网格，而是一张**入口卡**：站点名 / 域名 / 标签、`打开原站` + 镜像入口、**对应脚本**（从 tool 目录按 toolHints 匹配到的那个，可下载副本、可跳详情），以及 `导入会话` / `采集脚本` 两个按钮。

### 会话导入（让服务端带着你的登录态取源）

两条路，都只落本机：

| 方式 | 操作 |
|---|---|
| 采集脚本（推荐） | 点「采集脚本」下载配套油猴脚本 → 拖进 Tampermonkey → 在目标站点按 `Ctrl+Alt+S`（或菜单「⬆ 导出会话到 swipefeed」）→ 自动把 Cookie + localStorage 发回本机 |
| 手动粘贴 | 点「导入会话」→ 域名已自动填好 → 粘贴 `document.cookie`、按 `key=value` 粘贴 localStorage（如 `uu1d=...`）→ 保存 |

之后服务端对**登记过的域名（含子域）**在取源请求上自动带 `Cookie` / `Referer`；没登记时行为与之前完全一致。

接口：

| 接口 | 作用 |
|---|---|
| `GET /api/session` | 已导入列表（只回长度/键名，不回传值） |
| `POST /api/session` | 导入/更新（`{host, cookie, storage:[{key,value}], note}`，**只接受本机来源**） |
| `DELETE /api/session/:host`、`/api/session/all` | 删除单个 / 清空 |
| `GET /api/session/check?url=` | 查这个地址会不会带会话、命中哪个域名 |
| `GET /api/session/userscript?origin=` | 下载配套采集脚本 |

安全边界：`data/sessions.json` 已写进 `.gitignore`；写接口默认只放行 `127.0.0.1`（`SWIPEFEED_ALLOW_REMOTE_SESSION=1` 放开）；默认 7 天过期（`SWIPEFEED_SESSION_TTL_HOURS`）；列表不回传密文。

## tool 素材区整合

`F:\Freedom\tool` 的内容由 `/api/tools` 系列接口索引（扫描根可用 `SWIPEFEED_TOOL_DIRS` 覆盖，分号分隔）：

| 接口 | 作用 |
|------|------|
| `GET /api/tools` | 全部条目（`?kind=userscript\|text\|archive\|binary`、`?origin=`、`?refresh=1`） |
| `GET /api/tools/capabilities` | 能力聚合（VIP 强开 / HLS 下载 / 去广告 / 会话绕过 / IDOR …） |
| `GET /api/tools/sites` | tool 站点 × 处理方式对照表 |
| `GET /api/tools/sites/:id` | 单个站点条目 |
| `GET /api/tools/:id` | 单条工具详情 |
| `GET /api/browse`、`/api/browse/:id` | 查看页用：站点分组 + 实时分类 |
| `GET /api/img?u=&r=` | 封面代理（防盗链） |

站点分两类：

- **可直连（服务端实现）**：hsex、pektino、threego、haijiao、rou、x134、bilifun、netflav —— 查看页出卡片墙，点开即播。
- **需浏览器会话（只做索引）**：hstv / caoliu / xasian / 91·SP / javhub / 老王 / 春满院 / 艾薇社区 / 好妻网 / 套路影视 / 足控社 / 含羞草 / 镜像站 / 海角 posts / 知乎增强。这些站点把额度计数、uuid、VIP 判定放在页面里，脚本（`tool\*.user.txt`、`tampermonkey_scripts.zip` 里的 10 个）在浏览器侧运行；查看页给出站点入口、脚本信息与解析要点，不做服务端绕过。

Skipped: nv-pu-sa.pages.dev (images-only gallery), VIP-bypass mirrors.

## 部署到 NAS（子路径 + 固定端口）

线上入口（局域网）：**http://192.168.0.21:55080/swipefeed/**（域名 `nas.cylunex.top:55080`，HTTPS 走 `:55443`）。
容器内由 **supervisor** 托管，nginx 只做 `/swipefeed/` → `127.0.0.1:8790` 的反代。

| 项 | 值 |
|---|---|
| 主机 | `192.168.0.21:55022`（root） |
| 代码目录 | `/data/project/swipefeed-web` |
| supervisor | `/etc/supervisor/conf.d/swipefeed-web.conf`（program `swipefeed-web`） |
| 监听 | `HOST=127.0.0.1 PORT=8790`（只给 nginx 用） |
| 对外子路径 | `SWIPEFEED_BASE=/swipefeed` |
| 出口代理 | `PROXY_URL=http://192.168.0.110:7893`（NAS 直连目标站全部超时，必须走它） |
| 日志 | `/var/log/supervisor/swipefeed-web.log` |

**子路径适配**：`server.js` 按 `SWIPEFEED_BASE` 决定 `<base href>`、静态目录与路由挂载；
媒体代理（`/api/hls`、`/api/stream`、`/api/hj-hls`）生成给浏览器的地址也带同一前缀。
nginx 两种接法都兼容：`proxy_pass http://127.0.0.1:8790/;`（剥前缀）或 `…:8790;`（保留前缀）。

**改子路径**：只改 supervisor 里的 `SWIPEFEED_BASE`（如 `/nas/feed`）并同步 nginx location，然后
`supervisorctl restart swipefeed-web`，不需要改代码。

**发版**：

```bash
# 本机打包（排除 node_modules/.git/data）
tar -czf swipefeed-deploy.tar.gz --exclude=node_modules --exclude=.git --exclude=data .
scp -P 55022 swipefeed-deploy.tar.gz root@192.168.0.21:/root/
ssh -p 55022 root@192.168.0.21 '
  cd /data/project/swipefeed-web && tar -xzf /root/swipefeed-deploy.tar.gz &&
  supervisorctl restart swipefeed-web && supervisorctl status swipefeed-web'
```

依赖没变时不用 `npm install`；`data/`（站点开关、会话）不会被覆盖。
## Run

```bash
npm install
npm start          # 0.0.0.0:8787（业务路由同时挂在 / 与 /swipefeed）
npm run probe      # live-test all sources → TEST_RESULTS.md
npm run probe:tool # 只验 tool 来源的三个新源（pektino / 3GO / 海角）
```

Optional proxy: set `PROXY_URL` or `HTTPS_PROXY` / `HTTP_PROXY` (NAS: `http://192.168.0.110:7893`).

## Site settings

Dead/CF-hard sites default **off** (4tube, spankbang, pornone; orphans motherless/bitporno/noodlemagazine). Toggle in the UI **设置** panel without redeploy. Disabled sites are excluded from 单站列表 / 随机 / 分类混排 / 搜索.

Public-fetch hardening: undici ProxyAgent (`PROXY_URL`), browser-like UA/Accept-Language/Referer, one retry. No captcha solvers or WAF bypass.

## Modes

单站 / 随机 / 分类 / 搜索 — one HTML5 video, cover until first frame, tap pause, contain/cover toggle.

### 浏览器渲染源（慢源）

txxx / hqporner / beeg 的列表或播放地址只能跑 JS 才拿得到，服务端会起一次无头 Chrome：

- 单次 10–20 秒，结果缓存 10 分钟；/health 里的 rowser.queue 能看到当前占用（全局最多 2 个 Chrome）
- 前端对这三站**不做媒体预热和解析预取**，避免翻一屏就起好几次浏览器；设置面板里给它们标了「慢」
- SWIPEFEED_BROWSER_FETCH=0 关掉后，这三站会明确报「渲染失败」，不会静默返回空列表
### 搜索也是「轮流发牌」

/api/search 原来按源顺序逐个取、第一个源就把 30 条占满（所以一搜只出一个源）。
现在：每个源最多贡献 12 条 → 按轮次交替出卡 → 总量 30 条；总预算 9 秒、单源 7 秒超时，
跑无头浏览器的慢源（txxx / hqporner / beeg）不参与聚合搜索（单站搜索仍可用）。

实测：sian / milf / razil / korean 各 30 条，来自 **4–6 个源**，2–4 秒返回。

### UI 细节（2026-10）

- 封面：加载前是骨架渐变，失败/无图显示站点名占位，不再整块纯黑
- 加载态：12 秒还没起来就变成「加载超时 + 重试」，不再无限转圈
- 演示源换成本机可达的样片（Google 那个 bucket 在这里 403，之前首页卡住就是它）
- 设置面板：站点开关与「随机 / 刷流源」分区，后者可折叠（显示 N/总数 个源参与）
- 移动端：抽屉默认收起（跨 900px 断点自动收/展），卡片两列、封面 4:3
- 工具目录：油猴的 options.json / _storage.json 归为「附属文件」默认折叠，可一键显示；同名脚本合并并标 +N
- 键盘可达性：卡片/按钮/源选择都加了 :focus-visible 描边；页面允许双指缩放
### 随机模式怎么混排（`mode=random`）

不是「按权重排源 → 整页塞一个源」，而是**按源轮流发牌**：

- 每个源维护自己的待发池 + 已发集合；池子薄了单独补货（页号从随机起点递增，不会永远只要第一页）
- 出卡时每轮从不同源抽一张 → **相邻两张不会是同一源**（只要还有别的源可用）
- 池子越大越容易被抽到（Efraimidis–Spirakis），健康度差的源自然出得少，熔断中的源仍会跳过
- 冷启动并发补 6 个源（9 秒上限），之后后台继续补下一屏；热态单页 3–11ms
- 每源记住发过的卡片（最多 2000 条），翻页不重复

实测（连拉 15 页）：每页 **9–11 个源**、相邻同源 **0** 次、**0 空页**；累计 360 张去重 353（少量重复来自每源“轮完一遍后重置记忆”）。

### 选哪些源参与随机 / 刷流

设置面板（右上「设置」）里有 **随机 / 刷流源**：

- 列出当前启用的源，点一下切换是否参与混排；`全选` / `全不选` / `只留可用`（健康点=green）三个快捷键
- 选择存在本机 `localStorage`（`swipefeed:randomSites`），刷新/重启服务都还在
- 只影响随机混排，不改站点开关；请求侧就是 `GET /api/feed?mode=random&sites=a,b,c`，显式指定时会把每个源都先补满再发牌
- 全不选会自动保留 1 个，避免随机没得刷

App 侧的「刷流」用的是 `TubeRegistry` 里各源的开关，选择存在 App 内，与这里的网页选择互不影响。

## 源体检（2026-10 全量跑过一遍）

判据：列表有卡 → 能解析出播放地址 → 代理取字节 200/206。

**修复后可用的源（38 个启用中，随机/刷流可选）**：pornhub、xvideos、xnxx、youporn、youjizz、redtube、tube8、drtuber、tnaflix、empflix、porntube、thisvid、peekvids、hellporno、porntrex、3movs、porngo、zbporn、rule34video、perfectgirls、anyporn、superporn、bigfuck、porndoe、pornhat、sunporno、anysex、rou、x134、bilifun、hsex、aggrcine、threego、videoshub

这一轮修掉的：

| 站点 | 故障 | 修法 |
|---|---|---|
| sunporno | 列表 404 | 站点改版：列表 `/most-recent/` → `/trending/`，详情 `/videos/<id>/` → `/v/<id>/` |
| anysex | 列表 404 | 列表 `/latest-updates/` → `/videos/new/<p>/`，详情 `/videos/<id>/` → `/video/<id>/` |
| pornhat | 列表解析 0 条 | 详情页只剩 slug（`/video/<slug>/`），`hrefRe` 跟着改 |
| bilifun | 播放 404 | 清单地址末尾带 `\`，在 `streamFromMap` 统一裁掉 |
| anysex / pornhat / 3movs 等 | 误选 preview 小视频 | `looseMp4Hls` 改成打分：`/get_file/`、更高分辨率加权，`preview/screenshot/trailer` 减权 |

**暂时隐藏的源（设置面板里可再打开，原因也写在 note 里）**：

| 站点 | 原因 |
|---|---|
| xhamster / eporner | 上游 403，令牌绑 IP/会话，带 Referer 也拒 |
| netflav | 走 dmm，区域限制 403 |
| haijiao | 预览清单 AES key 解不开，需海角自己的密钥合成 |
| sexvid | `get_file` 直链 404（站点改版，待修） |
| beeg / txxx | 列表要 JS 渲染，静态抓不到 |
| redgifs | 需 API token |
| pektino / okxxx | 域名 TLS 握手直接失败（本机不可达） |
| porntop | 只有首页 11 条且无分页 |
| porntube | 播放页只有 `og:video` 预览片，正片走带 token 的私有 API |
| thisvid | `get_file` 直链只回 43 字节错误响应 |
| hqporner | 播放页走第三方 embed，解析未覆盖 |


查看 / 工具目录 — 按站点展开视频列表（网格、列表、分类、触底续页）。

Frontend base path: `/swipefeed/`.

Demo source always provides watchable Big Buck Bunny samples.
