/**
 * 海角 haijiao.com — 帖子型站点（来源 tool\ 的「m3u8提取+去广告」脚本与 LumenBox 已有实现）
 *
 * 接口对齐 LumenBox 的 HaijiaoCatalog / HaijiaoCodec：
 *   GET  /api/topic/hot/topics?page=N
 *   GET  /api/topic/node/topics?type=1&nodeId=0&page=N
 *   GET  /api/topic/node/news?page=N
 *   GET  /api/topic/<topicId>
 *   GET  /api/topic/att/<attId>
 *   GET  /api/topic/nodes_by_ver/v2?ver=0
 *   POST /api/attachment {id, resource_id, resource_type, line}
 * 响应 data 会套多层 base64，逐层剥到 JSON。
 *
 * 取值边界：公开预览（清单/预览 m3u8）直接给；需要登录态的完整源（POST /api/attachment
 * 的 VIP 线路）本模块不伪造账号，交由站点页面/脚本处理。
 */
import { DESKTOP_UA, fetchRaw } from '../util/http.js';

const HOST = 'https://www.haijiao.com';
const ORIGIN = HOST + '/';
const UA = DESKTOP_UA;
const TIMEOUT = 15000;
const MAX_PLAYLIST_BYTES = 512 * 1024;

const BASE_HEADERS = {
  Accept: 'application/json, text/plain, */*',
  'Accept-Language': 'zh-CN,zh;q=0.9',
  pcVer: '2',
  Origin: HOST,
  Referer: ORIGIN,
  'User-Agent': UA,
};

async function hjFetch(path, { method = 'GET', body = null } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT);
  try {
    const res = await fetchRaw(HOST + path, {
      method,
      headers: {
        ...BASE_HEADERS,
        ...(body ? { 'Content-Type': 'application/json; charset=utf-8' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      redirect: 'follow',
    });
    const raw = await res.text();
    if (!res.ok) throw Object.assign(new Error(`海角 HTTP ${res.status}`), { status: res.status });
    if (!raw.trim()) throw new Error('海角返回空内容');
    const root = decodePayload(raw);
    if (root.success === false || (typeof root.errorCode === 'number' && root.errorCode !== 0)) {
      throw new Error(String(root.message || '').trim().slice(0, 120) || '海角请求未成功');
    }
    return root;
  } finally {
    clearTimeout(timer);
  }
}

const B64_CHARS = /^[A-Za-z0-9+/=]+$/;

function peelOnce(text) {
  const compact = String(text || '').replace(/\s+/g, '');
  if (compact.length < 8 || compact.length % 4 !== 0) return null;
  if (!B64_CHARS.test(compact)) return null;
  try {
    return Buffer.from(compact, 'base64').toString('utf8');
  } catch {
    return null;
  }
}

/** 连续剥最多 5 层 base64，直到不是 base64 或剥出 JSON。 */
function peel(text) {
  let cur = String(text || '').trim().replace(/^"|"$/g, '');
  for (let i = 0; i < 5; i++) {
    const next = peelOnce(cur);
    if (!next) return cur;
    cur = next.trim();
  }
  return cur;
}

function parseJson(text) {
  const t = String(text || '').trim();
  try {
    return JSON.parse(t);
  } catch {
    return null;
  }
}

function decodePayload(raw) {
  const root = parseJson(raw.trim()) ?? {};
  if (typeof root.data === 'string') {
    const peeled = peel(root.data);
    const parsed = parseJson(peeled);
    if (parsed != null) root.data = parsed;
  }
  return root;
}

function imageUrl(raw) {
  const url = String(raw || '').trim();
  if (!/^https?:\/\//i.test(url)) return null;
  // 海角图片附件形如 xxx.jpg.txt（列表里是 _mini.jpg.txt）：去掉伪装后缀就是真图
  const stripped = url.replace(/\.(txt|hj)$/i, '');
  if (!/\.(jpg|jpeg|png|webp|gif)$/i.test(stripped)) return null;
  return stripped;
}

function secToDur() { return null; }

function topicToCard(row) {
  const id = row?.topicId != null ? String(row.topicId) : null;
  if (!id || id === 'null') return null;
  const attachments = Array.isArray(row.attachments) ? row.attachments : [];
  let cover = null;
  let attId = null;
  let preview = null;
  let hasVideoAtt = false;
  for (const att of attachments) {
    if (!att) continue;
    if (att.category !== 'video') {
      if (!cover) cover = imageUrl(att.coverUrl);
      continue;
    }
    hasVideoAtt = true;
    if (attId) continue;
    const candidate = att.id != null ? String(att.id) : null;
    if (!candidate || candidate === 'null') continue;
    attId = candidate;
    const remote = String(att.remoteUrl || '');
    if (/\.m3u8/i.test(remote)) preview = remote;
    cover = imageUrl(att.coverUrl) || cover;
  }
  const tags = (Array.isArray(row.tags) ? row.tags : [])
    .map((t) => t && (t.tagName || t.name))
    .filter(Boolean);
  const sale = row.sale || null;
  const moneyType = Number(
    (sale && sale.money_type != null ? sale.money_type : row.money_type) ?? 0,
  ) || 0;
  // 列表里 hasVideo 才是站点自己的判定；附件里出现 video 类也认
  const hasVideo = hasVideoAtt || attId != null || row.hasVideo === true;
  const videoType = !hasVideo ? '图文'
    : moneyType === 1 ? '金币片'
      : moneyType === 2 ? '钻石片'
        : sale ? '付费片' : '普通片';
  return {
    site: 'haijiao',
    id,
    title: String(row.title || `帖子 ${id}`).slice(0, 160),
    cover,
    duration: secToDur(),
    author: row.user?.nickname || null,
    views: Number(row.viewCount) > 0 ? String(row.viewCount) : null,
    aspect: 'portrait',
    pageUrl: `${HOST}/post/details?id=${id}`,
    hasVideo,
    videoType,
    tags: tags.slice(0, 6),
    _previewUrl: preview,
    _attachmentId: attId,
  };
}

function rowsOf(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.results)) return { rows: data.results, page: data.page };
  return { rows: [] };
}

function pageResult(root, page) {
  const data = root?.data;
  const { rows: rawRows, page: pageObj } = (() => {
    if (Array.isArray(data)) return { rows: data, page: null };
    if (data && Array.isArray(data.results)) return { rows: data.results, page: data.page || null };
    return { rows: [], page: null };
  })();
  // 列表里混着图文帖，保留帖子本身（hasVideo 标注视频/图文），播放时再按需解析
  const items = rawRows.map(topicToCard).filter(Boolean);
  const total = Number(pageObj?.total ?? rawRows.length) || 0;
  const limit = Number(pageObj?.limit) > 0 ? Number(pageObj.limit) : 20;
  const totalPage = Math.max(1, Math.ceil(total / limit));
  const current = Math.max(1, Number(pageObj?.page ?? page) || page);
  return {
    items,
    nextCursor: items.length && current < totalPage ? { page: current + 1 } : null,
    hasMore: items.length > 0 && current < totalPage,
    total,
    totalPage,
  };
}

const LIST_TYPES = {
  hot: (p) => `/api/topic/hot/topics?page=${p}`,
  latest: (p) => `/api/topic/node/topics?type=1&nodeId=0&page=${p}`,
  cream: (p) => `/api/topic/node/topics?type=3&nodeId=0&page=${p}`,
  original: (p) => `/api/topic/node/topics?type=7&page=${p}`,
  news: (p) => `/api/topic/node/news?page=${p}`,
};

/** 详情字段缓存：翻页/重试时同一帖不重复抓（haijiao 的封面/清单只在详情里）。 */
const detailCache = new Map(); // topicId -> { at, data }
const DETAIL_TTL_MS = 10 * 60 * 1000;

async function cachedDetail(topicId) {
  const hit = detailCache.get(topicId);
  if (hit && Date.now() - hit.at < DETAIL_TTL_MS) return hit.data;
  const root = await hjFetch(`/api/topic/${encodeURIComponent(topicId)}`);
  const data = root?.data || {};
  detailCache.set(topicId, { at: Date.now(), data });
  if (detailCache.size > 300) detailCache.delete(detailCache.keys().next().value);
  return data;
}

/** 展示用：给列表卡片补封面/真实清单标记（并发 4，失败静默）。 */
async function enrichCovers(items, limit = 12) {
  const slice = items.filter((c) => !c.cover).slice(0, limit);
  const queue = [...slice];
  const workers = Array.from({ length: 4 }, async () => {
    while (queue.length) {
      const card = queue.shift();
      if (!card) break;
      try {
        const d = await cachedDetail(card.id);
        const atts = Array.isArray(d.attachments) ? d.attachments : [];
        for (const att of atts) {
          const img = imageUrl(att?.coverUrl);
          if (img) { card.cover = img; break; }
        }
        for (const att of atts) {
          if (att?.category !== 'video') continue;
          const remote = String(att.remoteUrl || '');
          if (/\.m3u8/i.test(remote)) { card._previewUrl = card._previewUrl || remote; }
          if (!card._attachmentId && att.id != null) card._attachmentId = String(att.id);
        }
        card._detailLoaded = true;
      } catch { /* 单条失败不影响整页 */ }
    }
  });
  await Promise.all(workers);
}

/** 播放地址近况缓存：海角的 CDN 直链会过期，先探一下再交给播放器。 */
const urlHealth = new Map(); // url -> { ok, at }
const URL_HEALTH_TTL_MS = 5 * 60 * 1000;

async function urlAlive(url) {
  const hit = urlHealth.get(url);
  if (hit && Date.now() - hit.at < URL_HEALTH_TTL_MS) return hit.ok;
  let ok = false;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const res = await fetchRaw(url, {
      headers: {
        'User-Agent': UA,
        Accept: '*/*',
        Referer: ORIGIN,
        Origin: HOST,
        ...(/\.m3u8/i.test(url) ? { Range: 'bytes=0-2047' } : {}),
      },
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const code = res.status;
    const type = res.headers.get('content-type') || '';
    if (code === 200 || code === 206) {
      if (/\.m3u8/i.test(url)) {
        const text = await res.text();
        ok = /#EXTM3U/.test(text.slice(0, 2048));
      } else {
        ok = !/^text\/html/i.test(type);
      }
    }
  } catch { ok = false; }
  urlHealth.set(url, { ok, at: Date.now() });
  if (urlHealth.size > 200) urlHealth.delete(urlHealth.keys().next().value);
  return ok;
}

export const haijiao = {
  id: 'haijiao',
  name: '海角社区',
  displayName: '海角社区',

  async nextPage(cursor) {
    const page = Number(cursor?.page) || 1;
    const type = LIST_TYPES[cursor?.category] ? cursor.category : 'hot';
    const root = await hjFetch(LIST_TYPES[type](page));
    const result = pageResult(root, page);
    // 列表本身不带封面 → 只给前若干条抓一次详情（更快的做法是不抓，靠 UI 的「抓封面」开关）
    if (cursor?.enrich) await enrichCovers(result.items);
    return { ...result, _httpStatus: 200, _raw: JSON.stringify({ type, page }) };
  },

  async search(q, cursor) {
    const page = Number(cursor?.page) || 1;
    const keyword = q.trim();
    if (!keyword) return { items: [], nextCursor: null, hasMore: false };
    // 站点没有公开搜索接口：按热榜拉一页后本地过滤（标题/作者/标签）
    const base = await this.nextPage({ page, category: 'hot' });
    const kl = keyword.toLowerCase();
    const items = (base.items || []).filter((c) =>
      (c.title || '').toLowerCase().includes(kl)
      || (c.author || '').toLowerCase().includes(kl)
      || (c.tags || []).some((t) => t.toLowerCase().includes(kl)));
    return { ...base, items, note: 'hot-shelf-filter' };
  },

  async categoryPage(category, cursor) {
    const page = Number(cursor?.page) || 1;
    const cat = String(category || 'hot');
    // node:<id> → 分类节点；其余走固定货架
    const node = /^node:(\d+)$/.exec(cat);
    const path = node
      ? `/api/topic/node/topics?type=1&nodeId=${node[1]}&page=${page}`
      : (LIST_TYPES[cat] || LIST_TYPES.hot)(page);
    const root = await hjFetch(path);
    const result = pageResult(root, page);
    if (cursor?.enrich) await enrichCovers(result.items);
    return { ...result, _httpStatus: 200 };
  },

  /** 分类节点（node:<id> 形式的 category id），查看页用它出筛选项。 */
  async categories() {
    const shelves = [
      { id: 'hot', displayName: '热榜' },
      { id: 'latest', displayName: '最新' },
      { id: 'cream', displayName: '精华' },
      { id: 'original', displayName: '原创' },
      { id: 'news', displayName: '新闻' },
    ];
    try {
      const root = await hjFetch('/api/topic/nodes_by_ver/v2?ver=0');
      const data = root?.data;
      const list = Array.isArray(data) ? data : (data?.list || []);
      const nodes = [];
      for (const row of list) {
        if (Number(row?.parentId || 0) === 0) continue;
        const id = row?.nodeId != null ? String(row.nodeId) : null;
        if (!id) continue;
        nodes.push({ id: `node:${id}`, displayName: String(row.name || `节点 ${id}`) });
      }
      return [...shelves, ...nodes];
    } catch {
      return shelves;
    }
  },

  async resolve(card) {
    const playUrls = [];
    const add = (u) => {
      const v = String(u || '').trim();
      if (/^https?:\/\//i.test(v) && !playUrls.includes(v)) playUrls.push(v);
    };
    if (card._previewUrl) add(card._previewUrl);
    let title = card.title;
    let attId = card._attachmentId || null;
    try {
      const detail = await hjFetch(`/api/topic/${encodeURIComponent(card.id)}`);
      const d = detail?.data || {};
      title = String(d.title || title);
      const atts = Array.isArray(d.attachments) ? d.attachments : [];
      for (const att of atts) {
        if (att?.category !== 'video') continue;
        if (!attId && att.id != null) attId = String(att.id);
        add(att.remoteUrl);
      }
    } catch { /* 详情失败时用列表里带的预览 */ }
    if (attId) {
      try {
        const att = await hjFetch(`/api/topic/att/${encodeURIComponent(attId)}`);
        const collect = (node) => {
          if (!node) return;
          if (typeof node === 'string') { add(node); return; }
          if (Array.isArray(node)) { node.forEach(collect); return; }
          if (typeof node === 'object') {
            add(node.remoteUrl); add(node.url); collect(node.data);
          }
        };
        collect(att?.data);
      } catch { /* 附件通道失败不回滚已有预览 */ }
    }
    // 逐个探测，取第一个还活着的地址；全过期时给明确错误而不是让播放器空转
    let liveUrl = null;
    let livePreview = false;
    for (const u of playUrls) {
      // eslint-disable-next-line no-await-in-loop
      if (await urlAlive(u)) {
        liveUrl = u;
        livePreview = /(?:^|[/_.-])preview(?:[/_.-]|$)/i.test(u);
        break;
      }
    }
    const url = liveUrl || playUrls[0];
    if (!url) {
      throw new Error('海角：该帖没有公开播放地址（完整源需要登录态，请在站点页面处理）');
    }
    const preview = liveUrl ? livePreview : /(?:^|[/_.-])preview(?:[/_.-]|$)/i.test(url);
    // 清单是 AES-128，但清单里那把 key 解不开：交给 /api/hj-hls 做密钥合成 + 清单改写
    const isHls = /\.m3u8/i.test(url);
    const publicBase = (() => {
      const raw = String(process.env.SWIPEFEED_BASE || '/swipefeed').trim();
      if (!raw || raw === '/') return '';
      return (raw.startsWith('/') ? raw : '/' + raw).replace(/\/+$/, '');
    })();
    const playUrl = isHls ? `${publicBase}/api/hj-hls?u=${encodeURIComponent(url)}` : url;
    return {
      url: playUrl,
      type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
      qualities: isHls ? [{ label: '预览', url: playUrl }] : playUrls.map((u) => ({ label: '默认', url: u })),
      cover: card.cover || null,
      title,
      preview,
      expired: !liveUrl,
      note: liveUrl ? (preview ? 'preview-clip' : undefined) : 'all-urls-stale',
      expiresHintMs: Date.now() + 30 * 60_000,
    };
  },
};

export { MAX_PLAYLIST_BYTES };
