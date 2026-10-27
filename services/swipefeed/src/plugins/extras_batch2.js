import {
  makePlugin,
  kvsFlashvarsParser,
  qualityMapParser,
} from './factory.js';
import {
  fetchText,
  collectHrefCards,
  looseMp4Hls,
  streamFromMap,
  unescape,
} from '../util/http.js';

/** Second batch of public UGC tubes (2026-10-10). */

export const zbporn = makePlugin({
  id: 'zbporn', name: 'ZBPorn', base: 'https://zbporn.com',
  listUrl: (p) => (p <= 1 ? 'https://zbporn.com/latest-updates/' : `https://zbporn.com/latest-updates/${p}/`),
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q.trim().replace(/\s+/g, '-'));
    return p <= 1 ? `https://zbporn.com/search/${enc}/` : `https://zbporn.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => (p <= 1 ? `https://zbporn.com/categories/${cat}/` : `https://zbporn.com/categories/${cat}/${p}/`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?zbporn\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

export const rule34video = makePlugin({
  id: 'rule34video', name: 'Rule34Video', base: 'https://rule34video.com',
  listUrl: (p) => (p <= 1 ? 'https://rule34video.com/latest-updates/' : `https://rule34video.com/latest-updates/${p}/`),
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q.trim().replace(/\s+/g, '-'));
    return p <= 1 ? `https://rule34video.com/search/${enc}/` : `https://rule34video.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => (p <= 1 ? `https://rule34video.com/categories/${cat}/` : `https://rule34video.com/categories/${cat}/${p}/`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?rule34video\.com)?\/video\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

export const perfectgirls = makePlugin({
  id: 'perfectgirls', name: 'PerfectGirls', base: 'https://www.perfectgirls.xxx',
  listUrl: (p) => (p <= 1 ? 'https://www.perfectgirls.xxx/' : `https://www.perfectgirls.xxx/${p}/`),
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q.trim().replace(/\s+/g, '-'));
    return p <= 1 ? `https://www.perfectgirls.xxx/search/${enc}/` : `https://www.perfectgirls.xxx/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => (p <= 1 ? `https://www.perfectgirls.xxx/${cat}/` : `https://www.perfectgirls.xxx/${cat}/${p}/`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?perfectgirls\.(?:xxx|net))?\/video\/([0-9]+)\/?)["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

export const anyporn = makePlugin({
  id: 'anyporn', name: 'AnyPorn', base: 'https://anyporn.com',
  listUrl: (p) => (p <= 1 ? 'https://anyporn.com/' : `https://anyporn.com/?page=${p}`),
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q.trim().replace(/\s+/g, '-'));
    return p <= 1 ? `https://anyporn.com/search/${enc}/` : `https://anyporn.com/search/${enc}/${p}/`;
  },
  categoryUrl: (cat, p) => `https://anyporn.com/${cat}/${p > 1 ? p + '/' : ''}`,
  hrefRe: /href=["'](\/([0-9]{5,})\/?)["']/gi,
  watchParsers: [qualityMapParser, kvsFlashvarsParser],
});

export const superporn = makePlugin({
  id: 'superporn', name: 'SuperPorn', base: 'https://www.superporn.com',
  listUrl: (p) => (p <= 1 ? 'https://www.superporn.com/videos/latest' : `https://www.superporn.com/videos/latest/${p}`),
  searchUrl: (q, p) => {
    const enc = encodeURIComponent(q.trim());
    return `https://www.superporn.com/search?q=${enc}${p > 1 ? '&page=' + p : ''}`;
  },
  categoryUrl: (cat, p) => (p <= 1 ? `https://www.superporn.com/${cat}` : `https://www.superporn.com/${cat}?page=${p}`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?superporn\.com)?\/video\/([a-z0-9-]+))["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

export const sexvid = makePlugin({
  id: 'sexvid', name: 'SexVid', base: 'https://www.sexvid.xxx',
  listUrl: (p) => (p <= 1 ? 'https://www.sexvid.xxx/' : `https://www.sexvid.xxx/?page=${p}`),
  // public search routes 404 / ignore query — omit
  searchUrl: null,
  categoryUrl: (cat, p) => (p <= 1 ? `https://www.sexvid.xxx/c/${cat}/` : `https://www.sexvid.xxx/c/${cat}/${p}/`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?sexvid\.xxx)\/([a-z0-9-]+)\.html)["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

export const bigfuck = makePlugin({
  id: 'bigfuck', name: 'BigFuck', base: 'https://bigfuck.tv',
  listUrl: (p) => (p <= 1 ? 'https://bigfuck.tv/recent/' : `https://bigfuck.tv/recent/${p}/`),
  searchUrl: null,
  categoryUrl: (cat, p) => (p <= 1 ? `https://bigfuck.tv/category/${cat}/` : `https://bigfuck.tv/category/${cat}/${p}/`),
  hrefRe: /href=["']((?:https?:\/\/(?:www\.)?bigfuck\.tv)?\/video\/([0-9]+)\/[^"']*)["']/gi,
  watchParsers: [kvsFlashvarsParser],
});

/** PornDoe — listing via HTML cards; resolve via public /service/index JSON (signed MP4). */
export const porndoe = {
  id: 'porndoe',
  name: 'PornDoe',
  displayName: 'PornDoe',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? 'https://porndoe.com/' : `https://porndoe.com/videos?page=${page}`;
    const { status, body } = await fetchText(url, { referer: 'https://porndoe.com/' });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'porndoe',
      base: 'https://porndoe.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porndoe\.com)?\/watch\/([a-z0-9]+))["']/gi,
    });
    return {
      items,
      nextCursor: items.length ? { page: page + 1 } : null,
      hasMore: items.length > 0,
      _httpStatus: status,
      _raw: body,
    };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = `https://porndoe.com/videos/search?q=${enc}${page > 1 ? '&page=' + page : ''}`;
    const { status, body } = await fetchText(url, { referer: 'https://porndoe.com/' });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'porndoe',
      base: 'https://porndoe.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porndoe\.com)?\/watch\/([a-z0-9]+))["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async categoryPage(cat, cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1
      ? `https://porndoe.com/videos/category/${encodeURIComponent(cat)}`
      : `https://porndoe.com/videos/category/${encodeURIComponent(cat)}?page=${page}`;
    const { status, body } = await fetchText(url, { referer: 'https://porndoe.com/' });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'porndoe',
      base: 'https://porndoe.com',
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?porndoe\.com)?\/watch\/([a-z0-9]+))["']/gi,
    });
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const id = card.id;
    const api = `https://porndoe.com/service/index?device=desktop&page=video&id=${encodeURIComponent(id)}`;
    const { status, body } = await fetchText(api, {
      referer: card.pageUrl || `https://porndoe.com/watch/${id}`,
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (status >= 400) throw new Error(`PornDoe service HTTP ${status}`);
    let json;
    try {
      json = JSON.parse(body);
    } catch {
      throw new Error('PornDoe service JSON parse failed');
    }
    const sources = json?.payload?.player?.sources || {};
    const q = {};
    for (const list of [sources.mp4, sources.deo, sources.hls].filter(Boolean)) {
      for (const s of list) {
        const link = s.link || s.url || '';
        if (s.type === 'link' || !String(link).startsWith('http')) continue;
        q[String(s.name || s.height || 'default')] = unescape(link);
      }
    }
    const stream = streamFromMap(q);
    if (stream) return stream;
    // fallback: HTML watch page
    const { body: html } = await fetchText(card.pageUrl || `https://porndoe.com/watch/${id}`, {
      referer: 'https://porndoe.com/',
    });
    const loose = looseMp4Hls(html);
    if (loose && !/\.mp4\.jpg|preview/i.test(loose.url)) return loose;
    throw new Error('No playable stream for porndoe:' + id);
  },
};
