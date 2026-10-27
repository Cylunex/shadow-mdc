import { fetchText, unescape, firstMatch, streamFromMap, collectHrefCards, looseMp4Hls, absUrl } from '../util/http.js';
import { CAT_PATHS } from './categories.js';
import {
  makePlugin, mediaDefinitionsParser, xvideosStyleParser,
  encodingsParser, streamDataParser,
} from './factory.js';

/* ---------- xhamster (custom: initials JSON) ---------- */
const XH_BASE = 'https://xhamster.com';
function xhParseWatch(html) {
  const initials = firstMatch(/window\.initials\s*=\s*(\{[\s\S]*?\});/i, html)
    || firstMatch(/xplayerPreferences[^=]*=\s*(\{[\s\S]*?\});/i, html);
  if (initials) {
    try {
      const root = JSON.parse(initials);
      const sources = root.sources || root.videoModel?.sources || null;
      if (sources && typeof sources === 'object') {
        const q = {};
        for (const [k, v] of Object.entries(sources)) {
          const url = typeof v === 'string' ? v : v?.url;
          if (url?.startsWith('http')) q[k] = unescape(url);
        }
        const s = streamFromMap(q);
        if (s) return s;
      }
    } catch { /* fall through */ }
  }
  return looseMp4Hls(html);
}

const XH_HREF = /href=["']((?:https?:\/\/(?:[\w.-]+\.)?xhamster\.com)?\/videos\/([^"'?#]+))["']/gi;

export const xhamster = {
  id: 'xhamster', name: 'xHamster', displayName: 'xHamster',
  base: XH_BASE,
  parseListing(html) {
    return collectHrefCards(html, { site: 'xhamster', base: XH_BASE, hrefRe: XH_HREF });
  },
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${XH_BASE}/newest` : `${XH_BASE}/newest/${page}`;
    const { status, body } = await fetchText(url, { referer: XH_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'xhamster', base: XH_BASE,
      hrefRe: /href=["']((?:https?:\/\/(?:[\w.-]+\.)?xhamster\.com)?\/videos\/([^"'?#]+))["']/gi,
    });
    // also shorts
    if (!items.length) {
      const alt = collectHrefCards(body, {
        site: 'xhamster', base: XH_BASE,
        hrefRe: /href=["']((?:https?:\/\/(?:[\w.-]+\.)?xhamster\.com)?\/videos\/([^"'?#]+))["']/gi,
      });
      return { items: alt, nextCursor: alt.length ? { page: page + 1 } : null, hasMore: !!alt.length, _httpStatus: status, _raw: body };
    }
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0, _httpStatus: status, _raw: body };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const slug = q.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const url = page <= 1 ? `${XH_BASE}/search/${slug}` : `${XH_BASE}/search/${slug}?page=${page}`;
    const { status, body } = await fetchText(url, { referer: XH_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'xhamster', base: XH_BASE,
      hrefRe: /href=["']((?:https?:\/\/(?:[\w.-]+\.)?xhamster\.com)?\/videos\/([^"'?#]+))["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async categoryPage(category, cursor) {
    // 映射表只是别名；动态探测出来的任意 slug 也直接吃
    const slug = CAT_PATHS.xhamster[category] || String(category || '').trim();
    if (!slug) return { items: [], nextCursor: null, hasMore: false };
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${XH_BASE}/categories/${slug}` : `${XH_BASE}/categories/${slug}/${page}`;
    const { status, body } = await fetchText(url, { referer: XH_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'xhamster', base: XH_BASE,
      hrefRe: /href=["']((?:https?:\/\/(?:[\w.-]+\.)?xhamster\.com)?\/videos\/([^"'?#]+))["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const { body } = await fetchText(card.pageUrl, { referer: XH_BASE });
    const s = xhParseWatch(body);
    if (!s) throw new Error('No stream for ' + card.id);
    return s;
  },
};

/* ---------- xnxx ---------- */
export const xnxx = makePlugin({
  id: 'xnxx', name: 'XNXX', base: 'https://www.xnxx.com',
  categoryTemplate: '/tags/{slug}',
  listUrl: (p) => p <= 1 ? 'https://www.xnxx.com/best' : `https://www.xnxx.com/best/${p}`,
  searchUrl: (q, p) => {
    const slug = q.toLowerCase().replace(/\s+/g, '+');
    return p <= 1 ? `https://www.xnxx.com/search/${slug}` : `https://www.xnxx.com/search/${slug}/${p}`;
  },
  categoryUrl: (cat, p) => {
    const path = CAT_PATHS.xnxx[cat];
    if (!path) return 'https://www.xnxx.com/best';
    return p <= 1 ? `https://www.xnxx.com/${path}` : `https://www.xnxx.com/${path}/${p}`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?xnxx\.com)?\/video-([a-z0-9]+)\/[^"']*)["']/gi,
  watchParsers: [xvideosStyleParser],
});

/* ---------- spankbang ---------- */
export const spankbang = makePlugin({
  id: 'spankbang', name: 'SpankBang', base: 'https://spankbang.com',
  listUrl: (p) => p <= 1 ? 'https://spankbang.com/trending_videos/' : `https://spankbang.com/trending_videos/${p}/`,
  searchUrl: (q, p) => {
    const slug = q.toLowerCase().replace(/[^a-z0-9]+/g, '+');
    return p <= 1 ? `https://spankbang.com/s/${slug}/` : `https://spankbang.com/s/${slug}/${p}/`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.spankbang[cat] || cat;
    return p <= 1 ? `https://spankbang.com/category/${slug}/` : `https://spankbang.com/category/${slug}/${p}/`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?spankbang\.com)?\/([a-z0-9]+)\/video\/([^"']*))["']/gi,
  titleGroup: 3,
  watchParsers: [streamDataParser],
});

/* ---------- eporner ---------- */
async function epornerResolve(card) {
  const BASE = 'https://www.eporner.com';
  const { body } = await fetchText(card.pageUrl, { referer: BASE });
  const hash = firstMatch(/hash\s*[:=]\s*["']([a-f0-9]+)["']/i, body);
  if (hash) {
    try {
      const api = `${BASE}/api/v2/video/getsources?vid=${card.id}&hash=${hash}`;
      const { body: apiBody } = await fetchText(api, { referer: BASE });
      const data = JSON.parse(apiBody);
      const arr = Array.isArray(data) ? data : data.sources || [];
      const q = {};
      for (const o of arr) {
        const src = o.src || o.url;
        if (src?.startsWith('http')) q[String(o.label || o.quality || 'default')] = unescape(src);
      }
      const s = streamFromMap(q);
      if (s) return s;
    } catch { /* fall */ }
  }
  const loose = looseMp4Hls(body);
  if (loose) return loose;
  throw new Error('No stream for ' + card.id);
}

const epornerBase = makePlugin({
  id: 'eporner', name: 'Eporner', base: 'https://www.eporner.com',
  listUrl: (p) => p <= 1 ? 'https://www.eporner.com/' : `https://www.eporner.com/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.eporner.com/search/${enc}/` : `https://www.eporner.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.eporner[cat] || cat;
    return p <= 1 ? `https://www.eporner.com/cat/${slug}/` : `https://www.eporner.com/cat/${slug}/${p}/`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?eporner\.com)?\/video-([A-Za-z0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});
export const eporner = { ...epornerBase, resolve: epornerResolve };

/* ---------- youporn ---------- */
export const youporn = makePlugin({
  id: 'youporn', name: 'YouPorn', base: 'https://www.youporn.com',
  listUrl: (p) => p <= 1 ? 'https://www.youporn.com/' : `https://www.youporn.com/?page=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.youporn.com/search/?query=${enc}` : `https://www.youporn.com/search/?query=${enc}&page=${p}`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.youporn[cat] || cat;
    return p <= 1 ? `https://www.youporn.com/category/${slug}/` : `https://www.youporn.com/category/${slug}/?page=${p}`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?youporn\.com)?\/watch\/([0-9]+)(?:\/[^"']*)?)["']/gi,
  watchParsers: [mediaDefinitionsParser],
});

/* ---------- beeg ---------- */
const BEEG = 'https://beeg.com';
export const beeg = {
  id: 'beeg', name: 'Beeg', displayName: 'Beeg',
  categoryTemplate: '/tags/{slug}',
  async nextPage(cursor) {
    const page = cursor?.page ?? 0;
    const url = page <= 0 ? `${BEEG}/` : `${BEEG}/index/page-${page}`;
    const { status, body } = await fetchText(url, { referer: BEEG });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'beeg', base: BEEG,
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?beeg\.com)?\/([0-9]{5,}))["']/gi,
    }).map(c => ({ ...c, pageUrl: `${BEEG}/${c.id}` }));
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length, _httpStatus: status, _raw: body };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 0;
    const enc = encodeURIComponent(q);
    const url = page <= 0 ? `${BEEG}/search?q=${enc}` : `${BEEG}/search?q=${enc}&page=${page}`;
    const { status, body } = await fetchText(url, { referer: BEEG });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'beeg', base: BEEG,
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?beeg\.com)?\/([0-9]{5,}))["']/gi,
    }).map(c => ({ ...c, pageUrl: `${BEEG}/${c.id}` }));
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length };
  },
  async categoryPage(category, cursor) {
    const tag = CAT_PATHS.beeg[category];
    if (!tag) return { items: [], nextCursor: null, hasMore: false };
    const page = cursor?.page ?? 0;
    const url = page <= 0 ? `${BEEG}/tag/${tag}` : `${BEEG}/tag/${tag}/page-${page}`;
    const { status, body } = await fetchText(url, { referer: BEEG });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'beeg', base: BEEG,
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?beeg\.com)?\/([0-9]{5,}))["']/gi,
    }).map(c => ({ ...c, pageUrl: `${BEEG}/${c.id}` }));
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length };
  },
  async resolve(card) {
    const apiUrl = `https://api.beeg.com/api/v6/${Math.floor(Date.now() / 1000)}/video/${card.id}`;
    try {
      const { body } = await fetchText(apiUrl, { referer: BEEG });
      const obj = JSON.parse(body);
      const q = {};
      for (const [key, value] of Object.entries(obj)) {
        if (typeof value !== 'string') continue;
        let url = value;
        if (url.startsWith('//')) url = 'https:' + url;
        if (url.includes('{DATA_MARKERS}')) url = url.replace('{DATA_MARKERS}', 'data=pc.beeg.com');
        if (url.startsWith('http') && (/\d/.test(key) || key.includes('fl_cdn') || key.endsWith('p'))) {
          q[key.replace(/\D/g, '') || key] = url;
        }
      }
      const s = streamFromMap(q);
      if (s) return s;
    } catch { /* fall */ }
    const { body } = await fetchText(card.pageUrl, { referer: BEEG });
    const loose = looseMp4Hls(body);
    if (loose) return loose;
    throw new Error('No stream for ' + card.id);
  },
};

/* ---------- youjizz ---------- */
export const youjizz = makePlugin({
  id: 'youjizz', name: 'YouJizz', base: 'https://www.youjizz.com',
  listUrl: (p) => p <= 1 ? 'https://www.youjizz.com/' : `https://www.youjizz.com/page/${p}.html?s=new`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.youjizz.com/search/${enc}.html` : `https://www.youjizz.com/search/${enc}-${p}.html`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.youjizz[cat] || cat;
    return p <= 1 ? `https://www.youjizz.com/search/${slug}.html` : `https://www.youjizz.com/search/${slug}-${p}.html`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?youjizz\.com)?\/videos\/[^"']*?-([0-9]+)\.html)["']/gi,
  watchParsers: [encodingsParser],
});
