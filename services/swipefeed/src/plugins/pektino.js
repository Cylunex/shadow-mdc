/**
 * pektino.com — X(Twitter) 無料エロ動画保存ランキング
 *
 * 来源：F:\Freedom\tool 里的油猴脚本
 * ("视频站增强：去模糊 - 去广告 - 卡片直达视频"，也即 tool\test.txt)。
 * 脚本是在浏览器里从列表卡片数据里取直链；这里把同样的活放到服务端：
 * 列表页取 id → 详情页挑出属于该作品的 video.twimg.com MP4。
 *
 * 说明：站点自身不暴露标题（详情页 og:title 就是 “<id> - 免费视频。”），
 * 所以标题默认用 id；如需更好看的标题可打开 enrich（额外抓详情页拿 og 字段）。
 */
import { fetchText, unescape, clean } from '../util/http.js';

const BASE = 'https://pektino.com';
const MAX_CARDS = 48;
const DEEP_MAX = 8;

/** 列表页 JSON-LD ItemList（前 10 条带绝对 url）。 */
function parseItemList(html) {
  const ids = [];
  for (const m of html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)) {
    let j;
    try { j = JSON.parse(m[1]); } catch { continue; }
    if (j['@type'] !== 'ItemList') continue;
    for (const el of j.itemListElement || []) {
      const id = /\/(?:[a-z]{2}(?:-[A-Z]{2})?\/)?movie\/([A-Za-z0-9_-]{6,24})/.exec(el.url || '');
      if (id) ids.push(id[1]);
    }
  }
  return ids;
}

/** 兜底：页面里所有 /movie/<id> 出现处，尽量就近取封面图。 */
function parseBareIds(html, max) {
  const out = [];
  const seen = new Set();
  const re = /\/movie\/([A-Za-z0-9_-]{6,24})/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    const at = m.index;
    const chunk = html.slice(Math.max(0, at - 400), at + 900);
    const thumb = /amplify_video_thumb\/(\d+)\/img\/([A-Za-z0-9_-]+)\.jpg/.exec(chunk);
    out.push({
      id,
      cover: thumb ? `https://pbs.twimg.com/amplify_video_thumb/${thumb[1]}/img/${thumb[2]}.jpg` : null,
    });
    if (out.length >= max) break;
  }
  return out;
}

function cardFor(id, cover, title) {
  return {
    site: 'pektino',
    id,
    title: title || `Movie ${id}`,
    cover: cover || null,
    duration: null,
    author: 'X/Twitter',
    aspect: 'portrait',
    pageUrl: `${BASE}/movie/${id}`,
  };
}

function parseCards(html, max = MAX_CARDS) {
  const items = [];
  const seen = new Set();
  const ldIds = parseItemList(html);
  const bare = parseBareIds(html, max * 2);
  const coverOf = new Map(bare.map((b) => [b.id, b.cover]));
  for (const id of ldIds) {
    if (seen.has(id)) continue;
    seen.add(id);
    items.push(cardFor(id, coverOf.get(id)));
    if (items.length >= max) break;
  }
  for (const b of bare) {
    if (items.length >= max) break;
    if (seen.has(b.id)) continue;
    seen.add(b.id);
    items.push(cardFor(b.id, b.cover));
  }
  return items;
}

/**
 * 详情页里同时挂着“相关视频”的 MP4，必须按文件名的 id 段筛出本条。
 * 例：id=nhbq_k5tuul3mm4_ → .../avc1/1080x1440/NHBQ_K5tuUl3Mm4_.mp4
 */
function pickVariant(html, id) {
  const needle = String(id).toLowerCase().replace(/[^a-z0-9]/g, '');
  const urls = [...new Set(
    [...html.matchAll(/https?:\/\/video\.twimg\.com[^"'\s\\)]+\.mp4[^"'\s\\)]*/gi)]
      .map((x) => x[0].replace(/\\u0026/g, '&')),
  )];
  let best = null;
  for (const u of urls) {
    const file = u.split('/').pop().split('?')[0].toLowerCase().replace(/[^a-z0-9]/g, '');
    if (needle && !file.includes(needle)) continue;
    const dim = /\/vid\/avc1\/(\d+)x(\d+)\//.exec(u);
    const score = dim ? Number(dim[1]) * Number(dim[2]) : 0;
    if (!best || score > best.score) best = { url: u, score };
  }
  if (best) return best.url;
  return urls.length === 1 ? urls[0] : null;
}

async function detailMeta(pageUrl) {
  const { status, body } = await fetchText(pageUrl, {
    referer: BASE + '/', desktop: true, headers: { Accept: 'text/html' },
  });
  if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
  return { status, body };
}

function pageResult(items, page, extra = {}) {
  return {
    items,
    nextCursor: items.length ? { page: page + 1 } : null,
    hasMore: items.length > 0,
    ...extra,
  };
}

/** 站点其实只认“最新一页/懒加载”，分页参数常被忽略：记住已见 id，避免无限重复同批。 */
const seenByCursor = new Map();
function trackSeen(key, items) {
  const seen = seenByCursor.get(key) || new Set();
  let fresh = 0;
  for (const c of items) {
    if (!seen.has(c.id)) { seen.add(c.id); fresh++; }
  }
  seenByCursor.set(key, seen);
  if (seenByCursor.size > 8) seenByCursor.delete(seenByCursor.keys().next().value);
  return fresh;
}

async function listFrom(url, page, { enrich = false, max = MAX_CARDS, seenKey = null } = {}) {
  const { status, body } = await fetchText(url, {
    referer: BASE + '/',
    desktop: true,
    headers: { Accept: 'text/html' },
  });
  if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
  const items = parseCards(body, max);
  if (enrich) await enrichCards(items);
  const fresh = seenKey ? trackSeen(seenKey, items) : items.length;
  const result = pageResult(items, page, { _httpStatus: status, _raw: body });
  // 没解析满一页、或全是重复内容 → 到头了，不再给 cursor
  if ((result.items || []).length < max || fresh === 0) {
    result.nextCursor = null;
    result.hasMore = false;
    result.note = fresh === 0 ? 'dup-page' : 'short-page';
  }
  return result;
}

/** 可选：抓详情页 og:image / og:title 给卡片补封面（限流：一次最多 DEEP_MAX 条）。 */
async function enrichCards(items) {
  const slice = items.slice(0, DEEP_MAX);
  await Promise.all(slice.map(async (card) => {
    try {
      const { body } = await fetchText(card.pageUrl, {
        referer: BASE + '/', desktop: true, headers: { Accept: 'text/html' },
      });
      const img = /property="og:image" content="([^"]+)"/.exec(body);
      if (img && !card.cover) card.cover = img[1];
      const t = /property="og:title" content="([^"]+)"/.exec(body);
      if (t) {
        const cleanTitle = clean(t[1].replace(/ - [^-]*$/, ''));
        if (cleanTitle && cleanTitle !== card.id) card.title = cleanTitle;
      }
      const d = /"duration":"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?"/.exec(body);
      if (d) card.duration = `${Number(d[1] || 0)}:${String(Math.round(Number(d[2] || 0))).padStart(2, '0')}`;
    } catch { /* 单条失败不影响整页 */ }
  }));
}

export const pektino = {
  id: 'pektino',
  name: 'Pektino',
  displayName: 'Pektino (X 排行)',

  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const enrich = !!(cursor && cursor.enrich);
    // 站点按语言前缀分页：/zh-CN?page=N
    const url = page <= 1 ? `${BASE}/` : `${BASE}/?page=${page}`;
    return listFrom(url, page, { enrich, seenKey: 'home' });
  },

  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = page <= 1 ? `${BASE}/search?q=${enc}` : `${BASE}/search?q=${enc}&page=${page}`;
    try {
      const res = await listFrom(url, page, { enrich: !!cursor?.enrich, seenKey: `q:${enc}` });
      if (res.items.length) return res;
    } catch { /* 搜索路由可能不存在，退化为首页过滤 */ }
    const all = await listFrom(page <= 1 ? `${BASE}/` : `${BASE}/?page=${page}`, page, { seenKey: 'home' });
    const ql = q.trim().toLowerCase();
    return pageResult(
      (all.items || []).filter((c) => (c.title || '').toLowerCase().includes(ql) || (c.id || '').toLowerCase().includes(ql)),
      page,
      { _httpStatus: all._httpStatus, note: 'search-fallback-home' },
    );
  },

  async categoryPage(category, cursor) {
    const page = cursor?.page ?? 1;
    const slug = String(category || '').trim();
    if (!slug) return this.nextPage(cursor);
    const url = page <= 1
      ? `${BASE}/category/${encodeURIComponent(slug)}`
      : `${BASE}/category/${encodeURIComponent(slug)}?page=${page}`;
    try {
      return await listFrom(url, page, { enrich: !!cursor?.enrich, seenKey: `c:${slug}` });
    } catch {
      return listFrom(`${BASE}/`, page, { seenKey: 'home' });
    }
  },

  async resolve(card) {
    const pageUrl = card.pageUrl || `${BASE}/movie/${card.id}`;
    const { status, body } = await detailMeta(pageUrl);
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const url = pickVariant(body, card.id);
    if (!url) throw new Error('No twimg MP4 for ' + card.id);
    const cover = card.cover || (() => {
      const img = /property="og:image" content="([^"]+)"/.exec(body);
      return img ? img[1] : null;
    })();
    const d = /"duration":"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?"/.exec(body);
    const duration = d
      ? `${Number(d[1] || 0)}:${String(Math.round(Number(d[2] || 0))).padStart(2, '0')}`
      : null;
    return {
      url,
      type: 'mp4',
      qualities: [{ label: 'mp4', url }],
      cover,
      duration,
      expiresHintMs: Date.now() + 45 * 60_000,
    };
  },
};

export { parseCards as __parseCards, pickVariant as __pickVariant, unescape };
