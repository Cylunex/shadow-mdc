import { fetchText, unescape, firstMatch, streamFromMap, collectHrefCards, absUrl, clean } from '../util/http.js';
import { CAT_PATHS } from './categories.js';

const BASE = 'https://www.xvideos.com';
const SITE = 'xvideos';

function parseListing(html, preferVertical = false) {
  const seen = new Set();
  const out = [];
  const blockRe = /<div[^>]*class=["'][^"']*thumb-block[^"']*["'][^>]*>[\s\S]*?<\/div>\s*<\/div>/gi;
  let block;
  while ((block = blockRe.exec(html)) !== null) {
    const b = block[0];
    const m = /href=["']((?:https?:\/\/(?:www\.)?xvideos\.com)?\/video\.([a-z0-9]+)(\/[^"']*)?)["']/i.exec(b);
    if (!m) continue;
    const id = m[2];
    if (seen.has(id)) continue;
    seen.add(id);
    const path = m[1];
    const title = clean(firstMatch(/title=["']([^"']+)["']/i, b)) ||
      clean(firstMatch(/class=["'][^"']*title[^"']*["'][^>]*>\s*<a[^>]*>([^<]+)/i, b)) ||
      `Video ${id}`;
    const coverRaw = firstMatch(/<img[^>]+(?:data-src|data-id|src)\s*=\s*["']([^"']+)["']/i, b);
    let cover = null;
    if (coverRaw && !coverRaw.startsWith('data:')) {
      cover = coverRaw.startsWith('//') ? 'https:' + coverRaw : coverRaw.startsWith('/') ? BASE + coverRaw : coverRaw;
    }
    const duration = firstMatch(/(?:duration["']?\s*[:>]\s*|class=["'][^"']*duration[^"']*["'][^>]*>)\s*([0-9:]+)/i, b);
    const aspect = preferVertical || /quickies/i.test(path) ? 'vertical' : 'horizontal';
    out.push({ site: SITE, id, title, cover, duration, author: null, aspect, pageUrl: absUrl(BASE, path).split('?')[0] });
  }
  if (!out.length) {
    return collectHrefCards(html, {
      site: SITE, base: BASE,
      hrefRe: /href=["']((?:https?:\/\/(?:www\.)?xvideos\.com)?\/video\.([a-z0-9]+)(\/[^"'\s]*)?)["']/gi,
    }).map(c => ({ ...c, aspect: preferVertical ? 'vertical' : 'unknown' }));
  }
  return out;
}

function parseWatch(html) {
  const q = {};
  const hls = firstMatch(/html5player\.setVideoHLS\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  const high = firstMatch(/html5player\.setVideoUrlHigh\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  const low = firstMatch(/html5player\.setVideoUrlLow\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  if (hls) q.hls = unescape(hls);
  if (high) q.high = unescape(high);
  if (low) q.low = unescape(low);
  if (!Object.keys(q).length) {
    const re = /(?:setVideoUrl\w+|contentUrl)\s*[\(=]\s*['"](https?:\/\/[^'"]+\.(?:mp4|m3u8)[^'"]*)['"]/gi;
    let m, i = 0;
    while ((m = re.exec(html)) !== null) q[`q${i++}`] = unescape(m[1]);
  }
  return streamFromMap(q);
}

export const xvideos = {
  id: SITE, name: 'XVideos', displayName: 'XVideos',
  async nextPage(cursor) {
    const page = cursor?.page ?? 0;
    const useQuickies = page % 2 === 1;
    const listPage = Math.floor(page / 2);
    let url;
    if (useQuickies && listPage === 0) url = `${BASE}/tags/quickies`;
    else if (useQuickies) url = `${BASE}/tags/quickies/${listPage}`;
    else if (listPage <= 0) url = `${BASE}/`;
    else url = `${BASE}/new/${listPage}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body, useQuickies);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0, _httpStatus: status, _raw: body };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 0;
    const enc = encodeURIComponent(q.trim()).replace(/\+/g, '%20');
    const url = page <= 0 ? `${BASE}/?k=${enc}` : `${BASE}/?k=${enc}&p=${page}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body, false);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async categoryPage(category, cursor) {
    // 映射表只是别名；动态探测到的 slug 也直接吃。
    // 注意：/c/ 是旧路径（现在 404），现行可用形状是 /tags/<小写 slug>，
    // 与 CATEGORY_TEMPLATES.xvideos 保持一致，保证“探测到的分类”与“过滤用的 URL”是同一形状。
    const mapped = CAT_PATHS.xvideos[category];
    const slug = mapped || String(category || '').trim().toLowerCase().replace(/\s+/g, '-');
    if (!slug) return { items: [], nextCursor: null, hasMore: false };
    const page = cursor?.page ?? 0;
    const url = page <= 0 ? `${BASE}/tags/${slug}` : `${BASE}/tags/${slug}/${page + 1}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body, false);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const { body } = await fetchText(card.pageUrl, { referer: BASE });
    const stream = parseWatch(body);
    if (!stream) throw new Error('No playable stream for ' + card.id);
    return stream;
  },
};
