/**
 * tool-sites.js — `tool\` 素材区里每个站点 ↔ 处理方式 的对照表。
 *
 * 站点分两种：
 *  · server —— 已经在 swipefeed-web 里实现为可直连源（列表 / 搜索 / 解析），
 *              查看页可以直接出卡片墙并按站点播放。
 *  · browser —— 站点需要页面会话（uuid / 登录态 / 名额计数在客户端），
 *              查看页只做索引：站点入口 + 对应油猴脚本 + 解析要点。
 *
 * 这里的 toolId 对应 /api/tools 返回的条目 id（可用 SWIPEFEED_TOOL_DIRS 调整扫描根）。
 */

export const SERVER_SITE_IDS = ['hsex', 'pektino'];

export const TOOL_SITES = [
  /* ── 已是服务端源 ─────────────────────────────────────────────── */
  {
    id: 'hsex',
    name: '好色TV',
    host: 'hsex.tv',
    mode: 'server',
    sourceId: 'hsex',
    alsoSourceIds: ['theporny'],
    tags: ['国产自拍', '每日更新', 'CDN HLS'],
    notes: '列表/搜索/分类均可直连；解析取页面 <source> / m3u8。与 theporny 同架构。',
  },
  {
    id: 'pektino',
    name: 'Pektino（X 排行）',
    host: 'pektino.com',
    mode: 'server',
    sourceId: 'pektino',
    tags: ['推特视频', '排行', '20 个分类'],
    notes: '列表走 JSON-LD ItemList + 页面里的 /movie/<id>（去重后出卡片）；解析从详情页挑出属于该作品的 video.twimg.com MP4 —— 这一步原本由 tool 里的「视频站增强」脚本在浏览器里做。分类页 /category/<slug> 是客户端渲染，服务端拿不到条目。',
    toolHints: ['视频站增强'],
    categories: [
      'anime', 'beautiful-girl', 'big-sister', 'cosplay', 'fellatio', 'female-pervert',
      'gay', 'hamedori', 'jk', 'kyonyu', 'lolita', 'married-woman', 'masturbation',
      'orgy', 'outdoor', 'personal-filming', 'shaved', 'sm', 'small-breasts', 'titjob',
    ],
  },

  {
    id: 'threego',
    name: '3GO 国产自拍',
    host: 'media.3go.fun',
    mode: 'server',
    sourceId: 'threego',
    tags: ['MediaCMS', '原画 MP4', '分类'],
    categories: ['国产自拍', '麻豆探花'],
    notes: 'MediaCMS 实例：/api/v1/media 直接出列表与计数，详情接口给 original_media_url（原画 MP4）。',
    toolHints: ['nsfw_web_script'],
    mirrors: ['tube.3go.fun'],
  },
  {
    id: 'haijiao',
    name: '海角社区',
    host: 'haijiao.com',
    mode: 'server',
    sourceId: 'haijiao',
    tags: ['帖子型', '多层 base64', '公开预览'],
    categories: ['hot', 'latest', 'cream', 'original', 'news'],
    notes: '/api/topic/* 出列表与分类节点（响应 data 套多层 base64，已逐层剥）；播放取列表/详情里的预览 m3u8，需要登录态的完整源不在本工具里做。',
    toolHints: ['m3u8提取'],
  },

  {
    id: 'videoshub',
    name: 'VideosHub 视频库',
    host: 'd26fh0cuir1thg.r78k4dw47n.asia',
    mode: 'server',
    sourceId: 'videoshub',
    download: { quality: '720p', qualities: ['720p', '1080p'] },
    tags: ['GitHub 项目整合', 'protobuf 接口', 'HMAC 签名清单', 'AES-128 下载'],
    notes: '整合 moluhualuo/-VIP-（VideosHub 下载器）的 Python 逻辑：设备登录 + X-Sign 请求签名、预览地址 → HMAC 签名完整清单、AES-128-CBC 分片解密。播放交给浏览器 hls.js（清单带 ACAO:*），要看整片用卡片的「下载」走 /api/download。',
    toolHints: ['VideosHub'],
  },

  /* ── 需要浏览器会话 / 页面上下文 ─────────────────────────────── */
  {
    id: 'rou',
    name: '肉視頻',
    host: 'rou.video',
    mode: 'server',
    sourceId: 'rou',
    tags: ['PNG 伪装 HLS', '去广告'],
    notes: '已在源里实现（含 PNG 伪装 HLS 解包）。tool 目录另有去广告/下载脚本可叠加使用。',
    toolHints: ['rou.video 去广告'],
  },
  {
    id: 'x134',
    name: '134X長視頻',
    host: '134x.com',
    mode: 'server',
    sourceId: 'x134',
    tags: ['推特长视频', 'SSR'],
    notes: '已在源里实现（Twitter MP4）。',
  },
  {
    id: 'bilifun',
    name: 'Bili影院',
    host: 'bili-fun.com',
    mode: 'server',
    sourceId: 'bilifun',
    tags: ['JAV 中字'],
    notes: '已在源里实现（页面 m3u8）。',
  },
  {
    id: 'netflav',
    name: 'Netflav',
    host: 'netflav.com',
    mode: 'server',
    sourceId: 'netflav',
    tags: ['番号库', '中文字幕'],
    notes: '已在源里实现；部分条目只有磁力，无免费直链属正常。',
  },
  {
    id: 'hstv',
    name: '好色TV hstv',
    host: 'hstv.fun',
    mode: 'browser',
    tags: ['SevenVideos', '热度保护', '每日限额'],
    notes: '站点是 SevenVideos 应用：额度计数在客户端 localStorage，播放源由七视频网关返回 AES 密文，网关对伪造 uuid 直接返回 {"blocked":true}。查看页给出站点入口与脚本，不做服务端绕过。',
    toolHints: ['hstv-vip-unlock'],
    mirrors: ['hstv.pro', 'hstv.us'],
  },
  {
    id: 'caoliu',
    name: '草榴 caoliu',
    host: 'caoliu.io',
    mode: 'browser',
    tags: ['SevenVideos', '24h 5 个限制', 'VIP 线路'],
    notes: '与 hstv 同一套 SevenVideos 应用（主包 hash 相同），限制同为前端计数 + 网关密文源。',
    toolHints: ['caoliu-vip-unlock'],
  },
  {
    id: 'xasian',
    name: 'Xasian',
    host: 'xasian.org',
    mode: 'browser',
    tags: ['SevenVideos', '120 秒试看'],
    notes: '同为 SevenVideos 应用（首页与 hstv 同体积壳），免 VIP 突破 120 秒试看的逻辑在脚本里，需要页面上下文。',
    toolHints: ['nsfw_web_script'],
  },
  {
    id: 'sp2026',
    name: '91 / SP 系',
    host: '91.9p9.xyz',
    mode: 'browser',
    tags: ['VIP 限制', '原画直链'],
    notes: '脚本模块 1/6：绕过 VIP 限制提取原画/高清 MP4 直链并播放下载。',
    toolHints: ['nsfw_web_script'],
    mirrors: ['up.sp2026.com', 'www.91porn.com'],
  },
  {
    id: 'javhub',
    name: 'Javhub',
    host: 'javhub.net',
    mode: 'browser',
    tags: ['观看限额', '流式下载'],
    notes: '脚本模块 3/6：突破观看限额，免会员全片流式下载；站点对本机返回 403（Cloudflare）。',
    toolHints: ['nsfw_web_script'],
  },
  {
    id: 'laowang',
    name: '老王论坛',
    host: 'laowang.vip',
    mode: 'browser',
    tags: ['论坛', '免金币', 'm3u8'],
    notes: '脚本模块 2/6：免金币读取付费区视频 m3u8。',
    toolHints: ['nsfw_web_script'],
  },
  {
    id: 'chunman4',
    name: '春满院',
    host: 'chunman4.com',
    mode: 'browser',
    tags: ['附件枚举', 'AES-128', '无鉴权 /remote/'],
    notes: '脚本模块 6/6：付费帖由截图路径推导 m3u8/种子，AES-128 分片解密；附件移动 API 枚举 + /remote/ 直链。',
    toolHints: ['nsfw_web_script'],
  },
  {
    id: 'avjb',
    name: '艾薇社区',
    host: 'avjb.com',
    mode: 'browser',
    tags: ['VIP 破解', '手动触发'],
    notes: '脚本把播放直链从页面数据里取出后喂给自建播放器；需要页面上下文（同域 XHR）。',
    toolHints: ['艾薇社区破解VIP视频免费看', 'nsfw_web_script'],
    mirrors: ['bav53.cc'],
  },
  {
    id: 'hqw',
    name: '好妻网 HQW',
    host: '6vg4fgxe5.com',
    mode: 'browser',
    tags: ['钻石/VIP 强开', '侧栏下载'],
    notes: '域名轮换（另有 haoqi7.com 等）。脚本逻辑：会员专区点击门绕过 + 播放页全片强开 + 侧栏高并发下载。',
    toolHints: ['好妻网'],
    mirrors: ['haoqi7.com', 'www.hqwhqw.com'],
  },
  {
    id: 'taolu',
    name: '套路 / 桃鹿影视',
    host: 'taoluyingshi.com',
    mode: 'browser',
    tags: ['MacCMS Pro', '多线路直链'],
    notes: '本机实测：详情页/类型页都返回“没有权限访问此数据，请升级会员”，列表页可读、播放链路被登录态拦住。脚本按 vod/player 旁路取直链。',
    toolHints: ['套路影视'],
    mirrors: ['taolu.cyou', 'taolurun.com', 'taoluyingshi.vip'],
  },
  {
    id: 'h5c',
    name: '足控社 h5c',
    host: 'h5c.zks01.cc',
    mode: 'browser',
    tags: ['IDOR', 'AES-HLS'],
    notes: 'detail 泄露 videoInfo.user_id，用 getVideoStream?uid=owner 拉完整 AES-HLS。域名通配（h5c.*）。',
    toolHints: ['足控社'],
    mirrors: ['zukong.cc'],
  },
  {
    id: 'hanxiucao',
    name: '含羞草',
    host: '（通配 /play/video/）',
    mode: 'browser',
    sourceId: 'hanxiucao',
    tags: ['响应改写', '试看计数', '需登录态'],
    notes: '同域 getPreUrl 扩窗 + 响应改写：伪造 getUrl(isVip/isBuy/canPlay)，改 getInfo/getList/user 字段，重置试看计数。接口已复现：POST 体 = {endata, ents}，AES-256-CBC（固定 key，IV=key[0:16]）+ base64，响应是明文 JSON；/base/getConfig 可取配置，但 /videos/getList 在无 Auth 时恒为空，且站点没有游客/设备登录接口 —— 服务端已做成「带你自己 token」的可选源（默认关闭），在「会话」面板给 API 域名导入 Auth 即可。',
    toolHints: ['含羞草'],
  },
  {
    id: 'mirror-cc',
    name: '镜像站（HLS 下载）',
    host: 'mrds.com / mrds100.com / *.cc 轮换',
    mode: 'server',
    sourceId: 'mrds',
    tags: ['m3u8 解密', 'ffmpeg.wasm 封装'],
    notes: '已接成服务端源「每日大赛（镜像站）」：列表走 /page/<N>/，详情页里 "video":{"url":…m3u8} 直接取，解掉脚本里的 \\/ 转义即可；脚本里那套 ffmpeg.wasm 封装只用于下载，播放不需要。',
    toolHints: ['镜像站 HLS 下载'],
  },
  {
    id: 'haijiao-posts',
    name: '海角 · 帖子页',
    host: '*.haijiao.com',
    mode: 'browser',
    tags: ['帖子页', 'm3u8 捕获', '播放器注入'],
    notes: '服务端列表/分类已接入（另见「海角社区」条目）；帖子页里的完整清单需要页面上下文与登录态：1 自动屏蔽广告 2 捕获 m3u8 3 内置 HLS 引擎 + 强力播放器节点注入兜底。',
    toolHints: ['m3u8提取'],
  },
  {
    id: 'zhihu',
    name: '知乎增强',
    host: 'zhihu.com',
    mode: 'browser',
    tags: ['非视频站', '界面增强'],
    notes: '与站点接入无关的通用增强脚本，仅登记备查。',
    toolHints: ['Zhihu enhancement'],
  },
];

export function getToolSite(id) {
  return TOOL_SITES.find((s) => s.id === id) || null;
}

export function listToolSites() {
  return TOOL_SITES;
}

/** 站点 × 处理方式 汇总。 */
export function toolSiteSummary() {
  const server = TOOL_SITES.filter((s) => s.mode === 'server');
  const browser = TOOL_SITES.filter((s) => s.mode === 'browser');
  return {
    total: TOOL_SITES.length,
    server: server.map((s) => s.id),
    browser: browser.map((s) => s.id),
  };
}
