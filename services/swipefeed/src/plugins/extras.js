import { makePlugin, mediaDefinitionsParser, xvideosStyleParser, encodingsParser } from './factory.js';
import { looseMp4Hls, firstMatch, unescape, streamFromMap, fetchText, collectHrefCards } from '../util/http.js';
import { CAT_PATHS } from './categories.js';
import { renderHtml, capturePage } from '../util/browser-fetch.js';

/** Extra tube plugins — listing via href regex, resolve via common parsers / loose mp4. */

export const redtube = makePlugin({
  id: 'redtube', name: 'RedTube', base: 'https://www.redtube.com',
  listUrl: (p) => p <= 1 ? 'https://www.redtube.com/' : `https://www.redtube.com/?page=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.redtube.com/?search=${enc}` : `https://www.redtube.com/?search=${enc}&page=${p}`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.redtube?.[cat] || cat;
    return p <= 1 ? `https://www.redtube.com/redtube/${slug}` : `https://www.redtube.com/redtube/${slug}?page=${p}`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?redtube\.com)?\/([0-9]{5,})(?:\/|\?|"|'))/gi,
  watchParsers: [mediaDefinitionsParser],
});

export const tube8 = makePlugin({
  id: 'tube8', name: 'Tube8', base: 'https://www.tube8.com',
  listUrl: (p) => p <= 1 ? 'https://www.tube8.com/newest.html' : `https://www.tube8.com/newest.html?page=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.tube8.com/search.html?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => {
    const slug = CAT_PATHS.tube8?.[cat] || cat;
    return p <= 1 ? `https://www.tube8.com/category/${slug}/` : `https://www.tube8.com/category/${slug}/${p}/`;
  },
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?tube8\.com)?\/porn-video\/([0-9]+)\/?)["']/gi,
  watchParsers: [mediaDefinitionsParser],
});

/**
 * TXXX：站点是 SPA，列表和播放地址都要跑 JS。
 *   · 列表：headless Chrome 渲染 /videos/ 后抓 `/videos/<id>/<slug>/` 链接（结果缓存 10 分钟）
 *   · 解析：CDP 抓播放器真正请求的媒体地址，优先 `_hq.mp4`/`/get_file/`，排除 `_tr.mp4`（预告）
 * 代价是每次约 10–20 秒，靠缓存摊薄；Chrome 不可用时抛错而不是静默空列表。
 */
const TXXX_REF = 'https://www.txxx.com/';

function txxxPickMedia(media) {
  const list = (media || []).filter((u) => /^https?:/i.test(u));
  const score = (u) => {
    let s = 0;
    if (/\/get_file\//i.test(u)) s += 8;
    if (/_hq\.mp4/i.test(u)) s += 6;
    if (/_(\d{3,4})p\.mp4/i.test(u)) s += 4;
    if (/\.m3u8/i.test(u)) s += 2;
    if (/_tr\.mp4|_preview|_trailer|sprite|thumb/i.test(u)) s -= 10;
    return s;
  };
  return list.map((u) => ({ u, s: score(u) })).filter((x) => x.s > -10).sort((a, b) => b.s - a.s)[0]?.u || null;
}

export const txxx = {
  id: 'txxx', name: 'TXXX', displayName: 'TXXX',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? 'https://www.txxx.com/videos/' : `https://www.txxx.com/videos/?page=${page}`;
    const html = await renderHtml(url, { timeoutMs: 32000 });
    if (!html) throw new Error('TXXX 渲染失败（本机 Chrome 不可用或超时）');
    const items = collectHrefCards(html, {
      site: 'txxx', base: 'https://www.txxx.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?txxx\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length, _rendered: true };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(String(q || '').trim());
    const url = `https://www.txxx.com/search/?q=${enc}` + (page > 1 ? `&page=${page}` : '');
    const html = await renderHtml(url, { timeoutMs: 32000 });
    if (!html) throw new Error('TXXX 渲染失败（本机 Chrome 不可用或超时）');
    const items = collectHrefCards(html, {
      site: 'txxx', base: 'https://www.txxx.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?txxx\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length, _rendered: true };
  },
  async categoryPage(cat, cursor) {
    const page = cursor?.page ?? 1;
    const slug = encodeURIComponent(String(cat || '').trim());
    const url = `https://www.txxx.com/categories/${slug}/` + (page > 1 ? `${page}/` : '');
    const html = await renderHtml(url, { timeoutMs: 32000 });
    if (!html) throw new Error('TXXX 渲染失败（本机 Chrome 不可用或超时）');
    const items = collectHrefCards(html, {
      site: 'txxx', base: 'https://www.txxx.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?txxx\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: !!items.length, _rendered: true };
  },
  async resolve(card) {
    const pageUrl = card.pageUrl && /^https?:/i.test(card.pageUrl)
      ? card.pageUrl
      : `${TXXX_REF}videos/${card.id}/`;
    // 第一次 22 秒；抓不到就再给一次更长的等待（播放器偶发懒加载）
    let shot = await capturePage(pageUrl, { timeoutMs: 40000, waitMs: 22000 });
    let url = txxxPickMedia(shot?.media);
    if (!url) {
      shot = await capturePage(pageUrl, { timeoutMs: 50000, waitMs: 30000 });
      url = txxxPickMedia(shot?.media);
    }
    if (!url) {
      // 最后退到预告片（_tr.mp4），至少能播而不是直接报错
      const preview = (shot?.media || []).find((u) => /_tr\.mp4|preview/i.test(u));
      if (preview) url = preview;
    }
    if (url) {
      return {
        url,
        type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
        qualities: [{ quality: /\.m3u8/i.test(url) ? 'hls' : 'auto', url }],
        referer: pageUrl,
      };
    }
    // 抓不到媒体就退回静态 HTML 的兜底
    if (shot?.html) {
      const loose = looseMp4Hls(shot.html);
      if (loose) return loose;
    }
    throw new Error('TXXX 未抓到播放地址（渲染或等待超时）');
  },
};

export const drtuber = makePlugin({
  id: 'drtuber', name: 'DrTuber', base: 'https://www.drtuber.com',
  listUrl: (p) => p <= 1 ? 'https://www.drtuber.com/' : `https://www.drtuber.com/${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.drtuber.com/search/videos/${enc}${p > 1 ? '/' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.drtuber.com/tags/${cat}${p > 1 ? '/' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?drtuber\.com)?\/video\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const tnaflix = makePlugin({
  id: 'tnaflix', name: 'TNAFlix', base: 'https://www.tnaflix.com',
  listUrl: (p) => p <= 1 ? 'https://www.tnaflix.com/new/' : `https://www.tnaflix.com/new/${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.tnaflix.com/search.php?what=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.tnaflix.com/${cat}${p > 1 ? '/' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?tnaflix\.com)?\/[^"']*?video([0-9]+)[^"']*)["']/gi,
  watchParsers: [],
});

export const empflix = makePlugin({
  id: 'empflix', name: 'EmpFlix', base: 'https://www.empflix.com',
  listUrl: (p) => p <= 1 ? 'https://www.empflix.com/new/' : `https://www.empflix.com/new/${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.empflix.com/search.php?what=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.empflix.com/${cat}${p > 1 ? '/' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?empflix\.com)?\/[^"']*?video([0-9]+)[^"']*)["']/gi,
  watchParsers: [],
});

export const porntube = makePlugin({
  id: 'porntube', name: 'PornTube', base: 'https://www.porntube.com',
  listUrl: (p) => p <= 1 ? 'https://www.porntube.com/' : `https://www.porntube.com/?page=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.porntube.com/search?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.porntube.com/tags/${cat}${p > 1 ? '?page=' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porntube\.com)?\/video\/([A-Za-z0-9_-]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const sunporno = makePlugin({
  id: 'sunporno', name: 'SunPorno', base: 'https://www.sunporno.com',
  listUrl: (p) => p <= 1 ? 'https://www.sunporno.com/trending/' : `https://www.sunporno.com/trending/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.sunporno.com/search/${enc}/${p > 1 ? p + '/' : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.sunporno.com/channels/${cat}/${p > 1 ? p + '/' : ''}`,
  // 站点改版：详情页变成 /v/<id>/<slug>/（旧模板还在用 /videos/<id>/）
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?sunporno\.com)?\/v\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const thisvid = makePlugin({
  id: 'thisvid', name: 'ThisVid', base: 'https://thisvid.com',
  listUrl: (p) => p <= 1 ? 'https://thisvid.com/latest-updates/' : `https://thisvid.com/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://thisvid.com/search/?q=${enc}` : `https://thisvid.com/search/${p}/?q=${enc}`;
  },
  categoryUrl: (cat, p) => `https://thisvid.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?thisvid\.com)?\/videos\/[^"']*?-([0-9]+)\/?)["']/gi,
  watchParsers: [],
});

export const hellporno = makePlugin({
  id: 'hellporno', name: 'HellPorno', base: 'https://hellporno.com',
  listUrl: (p) => p <= 1 ? 'https://hellporno.com/' : `https://hellporno.com/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://hellporno.com/search/?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://hellporno.com/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:(?:https?:)?\/\/(?:www\.|m\.)?hellporno\.com)?\/videos\/([a-z0-9-]+)\/?)["']/gi,
  watchParsers: [],
});

export const fourtube = makePlugin({
  id: '4tube', name: '4Tube', base: 'https://www.4tube.com',
  listUrl: (p) => p <= 1 ? 'https://www.4tube.com/videos' : `https://www.4tube.com/videos?p=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.4tube.com/search?q=${enc}${p > 1 ? '&p=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.4tube.com/videos/${cat}${p > 1 ? '?p=' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?4tube\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const porntop = makePlugin({
  id: 'porntop', name: 'PornTop', base: 'https://porntop.com',
  listUrl: (p) => p <= 1 ? 'https://porntop.com/latest-updates/' : `https://porntop.com/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://porntop.com/search/${enc}/` : `https://porntop.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => `https://porntop.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porntop\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const peekvids = makePlugin({
  id: 'peekvids', name: 'PeekVids', base: 'https://www.peekvids.com',
  listUrl: (p) => p <= 1 ? 'https://www.peekvids.com/' : `https://www.peekvids.com/?page=${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.peekvids.com/results?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.peekvids.com/${cat}${p > 1 ? '?page=' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?peekvids\.com)?\/v\/[^"']*?\/([A-Za-z0-9_-]{6,}))["']/gi,
  watchParsers: [],
});

/**
 * RedGifs：静态页面已经拿不到列表（SPA），走它自己的公开接口：
 *   GET /v2/auth/temporary           → 临时 token（约 1 小时）
 *   GET /v2/gifs/search?search_text=&order=trending&count=36&page=N
 *   GET /v2/gifs?ids=<id>            → 单条详情（含 urls.hd / urls.sd）
 * 视频是 MP4，可直接走 <video>；封面/时长/id 都在列表里给全。
 */
const REDGIFS_API = 'https://api.redgifs.com';
const REDGIFS_REF = 'https://www.redgifs.com/';
let redgifsToken = { value: '', expires: 0 };

async function redgifsAuth() {
  if (redgifsToken.value && Date.now() < redgifsToken.expires) return redgifsToken.value;
  const { status, body } = await fetchText(`${REDGIFS_API}/v2/auth/temporary`, {
    referer: REDGIFS_REF,
    headers: { Accept: 'application/json' },
  });
  if (status >= 400) throw Object.assign(new Error(`redgifs auth HTTP ${status}`), { status });
  const token = (() => {
    try { return JSON.parse(body).token || ''; } catch { return ''; }
  })();
  if (!token) throw new Error('redgifs auth: no token');
  redgifsToken = { value: token, expires: Date.now() + 45 * 60 * 1000 };
  return token;
}

async function redgifsJson(path) {
  const token = await redgifsAuth();
  const { status, body } = await fetchText(REDGIFS_API + path, {
    referer: REDGIFS_REF,
    headers: { Accept: 'application/json', Authorization: `Bearer ${token}` },
  });
  if (status >= 400) throw Object.assign(new Error(`redgifs HTTP ${status}`), { status, body });
  return JSON.parse(body);
}

function redgifsCard(g) {
  const urls = g.urls || {};
  const desc = g.description && String(g.description).trim();
  const fallback = [g.userName && `@${g.userName}`, g.contentType].filter(Boolean).join(' · ');
  return {
    site: 'redgifs',
    id: String(g.id || ''),
    title: desc || fallback || `RedGifs ${g.id}`,
    cover: urls.thumbnail || urls.poster || null,
    duration: g.duration ? formatDur(g.duration) : null,
    author: g.userName || null,
    pageUrl: `https://www.redgifs.com/watch/${g.id}`,
    aspect: g.width && g.height ? (g.height > g.width ? 'portrait' : 'landscape') : 'unknown',
    extra: { verified: !!g.verified, hasAudio: !!g.hasAudio },
  };
}

function formatDur(sec) {
  const total = Math.round(Number(sec) || 0);
  if (!total) return null;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export const redgifs = {
  id: 'redgifs', name: 'RedGifs', displayName: 'RedGifs',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const data = await redgifsJson(`/v2/gifs/search?search_text=&order=trending&count=36&page=${page}`);
    const items = (data.gifs || []).map(redgifsCard).filter((c) => c.id);
    const pages = Number(data.pages) || 0;
    return {
      items,
      nextCursor: items.length ? { page: page + 1 } : null,
      hasMore: items.length > 0 && (!pages || page < pages),
    };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(String(q || '').trim());
    const data = await redgifsJson(`/v2/gifs/search?search_text=${enc}&order=trending&count=36&page=${page}`);
    const items = (data.gifs || []).map(redgifsCard).filter((c) => c.id);
    return {
      items,
      nextCursor: items.length ? { page: page + 1 } : null,
      hasMore: items.length > 0,
    };
  },
  async categories() {
    const data = await redgifsJson('/v2/niches/trending?count=24');
    return (data.niches || []).map((n) => ({ slug: n.name, name: n.name, displayName: n.name }));
  },
  async categoryPage(cat, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(String(cat || '').trim());
    const data = await redgifsJson(`/v2/gifs/search?search_text=${enc}&order=trending&count=36&page=${page}`);
    const items = (data.gifs || []).map(redgifsCard).filter((c) => c.id);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const data = await redgifsJson(`/v2/gifs/${encodeURIComponent(card.id)}`);
    const g = data.gif || (data.gifs || [])[0];
    if (!g) throw new Error('redgifs: gif not found ' + card.id);
    const urls = g.urls || {};
    const stream = streamFromMap({ hd: urls.hd, sd: urls.sd, silent: urls.silent });
    if (stream) return stream;
    throw new Error('redgifs: no stream for ' + card.id);
  },
};

export const hqporner = makePlugin({
  id: 'hqporner', name: 'HQPorner', base: 'https://hqporner.com',
  listUrl: (p) => p <= 1 ? 'https://hqporner.com/' : `https://hqporner.com/${p}`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://hqporner.com/?q=${enc}${p > 1 ? '&p=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://hqporner.com/category/${cat}${p > 1 ? '/' + p : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?hqporner\.com)?\/hdporn\/([0-9]+)-[^"']*)["']/gi,
  watchParsers: [],
  // 播放地址在第三方 embed（mydaddy）里，静态 HTML 拿不到：
  // 用 CDP 打开详情页，记录它真正请求的 /library/*.mp4。
  async resolve(card) {
    const pageUrl = card.pageUrl && /^https?:/i.test(card.pageUrl)
      ? card.pageUrl
      : `https://hqporner.com/hdporn/${card.id}-x.html`;
    const shot = await capturePage(pageUrl, { timeoutMs: 35000, waitMs: 16000 });
    const mp4s = (shot?.media || []).filter((u) => /\.mp4(\?|$)/i.test(u));
    const url = mp4s.find((u) => /\/library\//i.test(u)) || mp4s[0];
    if (url) {
      return { url, type: 'mp4', qualities: [{ label: '默认', url }], referer: pageUrl };
    }
    throw new Error('HQPorner 未抓到播放地址（embed 渲染超时）');
  },
});

export const porntrex = makePlugin({
  id: 'porntrex', name: 'PornTrex', base: 'https://www.porntrex.com',
  listUrl: (p) => p <= 1 ? 'https://www.porntrex.com/latest-updates/' : `https://www.porntrex.com/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.porntrex.com/search/${enc}/` : `https://www.porntrex.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => `https://www.porntrex.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porntrex\.com)?\/video\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const threemovs = makePlugin({
  id: '3movs', name: '3Movs', base: 'https://www.3movs.com',
  listUrl: (p) => p <= 1 ? 'https://www.3movs.com/latest-updates/' : `https://www.3movs.com/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.3movs.com/search_videos/?q=${enc}` : `https://www.3movs.com/search_videos/${p}/?q=${enc}`;
  },
  categoryUrl: (cat, p) => `https://www.3movs.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?3movs\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const pornone = makePlugin({
  id: 'pornone', name: 'PornOne', base: 'https://pornone.com',
  listUrl: (p) => p <= 1 ? 'https://pornone.com/newest/' : `https://pornone.com/newest/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://pornone.com/search/?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => `https://pornone.com/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?pornone\.com)?\/[^"']*?\/([0-9]{4,})\/?)["']/gi,
  watchParsers: [],
});

export const anysex = makePlugin({
  id: 'anysex', name: 'AnySex', base: 'https://www.anysex.com',
  listUrl: (p) => p <= 1 ? 'https://anysex.com/videos/new/' : `https://anysex.com/videos/new/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.anysex.com/search/?q=${enc}` : `https://www.anysex.com/search/${p}/?q=${enc}`;
  },
  categoryUrl: (cat, p) => `https://www.anysex.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  // 站点改版：视频页变成 /video/<id>/<slug>/（旧模板是 /videos/）
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?anysex\.com)?\/video\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const pornhat = makePlugin({
  id: 'pornhat', name: 'PornHat', base: 'https://www.pornhat.com',
  listUrl: (p) => p <= 1 ? 'https://www.pornhat.com/' : `https://www.pornhat.com/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return `https://www.pornhat.com/search/${enc}/${p > 1 ? p + '/' : ''}`;
  },
  categoryUrl: (cat, p) => `https://www.pornhat.com/${cat}/${p > 1 ? p + '/' : ''}`,
  // 站点改版：视频页只有 slug（/video/<slug>/），不再带 -数字 后缀
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?pornhat\.com)?\/video\/([a-z0-9-]{6,})\/?)["']/gi,
  watchParsers: [],
});

export const okxxx = makePlugin({
  id: 'okxxx', name: 'OK.XXX', base: 'https://ok.xxx',
  listUrl: (p) => p <= 1 ? 'https://ok.xxx/latest-updates/' : `https://ok.xxx/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://ok.xxx/search/${enc}/` : `https://ok.xxx/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => `https://ok.xxx/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?ok\.xxx)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});

export const porngo = makePlugin({
  id: 'porngo', name: 'PornGo', base: 'https://www.porngo.com',
  listUrl: (p) => p <= 1 ? 'https://www.porngo.com/latest-updates/' : `https://www.porngo.com/latest-updates/${p}/`,
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q);
    return p <= 1 ? `https://www.porngo.com/search/${enc}/` : `https://www.porngo.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => `https://www.porngo.com/categories/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porngo\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [],
});
