import { fetchText, unescape, absUrl, clean, firstMatch, streamFromMap, collectHrefCards } from '../util/http.js';
import { CAT_PATHS } from './categories.js';

const BASE = 'https://www.pornhub.com';
const SITE = 'pornhub';

function parseListing(html) {
  const seen = new Set();
  const out = [];
  const blockRe = /<li[^>]*?(?:data-video-vkey|videoblock)[^>]*>[\s\S]*?<\/li>/gi;
  let block;
  while ((block = blockRe.exec(html)) !== null) {
    const b = block[0];
    const id =
      firstMatch(/data-video-vkey\s*=\s*["']([a-zA-Z0-9]+)["']/i, b) ||
      firstMatch(/view_video\.php\?viewkey=([a-zA-Z0-9]+)/i, b);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const title =
      clean(firstMatch(/title=["']([^"']+)["']/i, b)) ||
      clean(firstMatch(/class=["'][^"']*title[^"']*["'][^>]*>([^<]+)/i, b)) ||
      `Video ${id}`;
    const coverRaw = firstMatch(/<img[^>]+(?:data-src|data-mediumthumb|data-path|src)\s*=\s*["']([^"']+)["']/i, b);
    let cover = null;
    if (coverRaw && !coverRaw.startsWith('data:')) {
      cover = coverRaw.startsWith('//') ? 'https:' + coverRaw : coverRaw.startsWith('/') ? BASE + coverRaw : coverRaw;
    }
    const duration = firstMatch(/(?:class=["'][^"']*duration[^"']*["'][^>]*>)\s*([0-9:]+)/i, b);
    out.push({
      site: SITE, id, title, cover, duration, author: null, aspect: 'unknown',
      pageUrl: `${BASE}/view_video.php?viewkey=${id}`,
    });
  }
  if (!out.length) {
    return collectHrefCards(html, {
      site: SITE, base: BASE,
      hrefRe: /(?:href|data-related-url)\s*=\s*["']([^"']*view_video\.php\?viewkey=([a-zA-Z0-9]+)[^"']*)["']/gi,
    });
  }
  return out;
}

function parseWatch(html) {
  const q = {};
  const md = firstMatch(/(?:mediaDefinitions|media_definitions)\s*[:=]\s*(\[[\s\S]*?\])\s*[,;]/i, html);
  if (md) {
    try {
      const arr = JSON.parse(unescape(md));
      for (const o of arr) {
        const url = unescape(o.videoUrl || o.video_url || '');
        if (!url.startsWith('http')) continue;
        let quality = o.quality;
        if (Array.isArray(quality)) quality = quality[0];
        q[String(quality || 'default')] = url;
      }
    } catch { /* ignore */ }
  }
  if (!Object.keys(q).length) {
    const re = /"quality"\s*:\s*"(\d+)"\s*,\s*"videoUrl"\s*:\s*"([^"]+)"|"videoUrl"\s*:\s*"([^"]+)"\s*,\s*"quality"\s*:\s*"(\d+)"/gi;
    let m;
    while ((m = re.exec(html)) !== null) {
      const label = m[1] || m[4];
      const url = unescape(m[2] || m[3]);
      if (url.startsWith('http')) q[label] = url;
    }
  }
  const direct = Object.fromEntries(
    Object.entries(q).filter(([, u]) => /\.m3u8|\.mp4|get_media/i.test(u) === false || /\.m3u8|\.mp4/i.test(u)),
  );
  return streamFromMap(Object.keys(direct).length ? direct : q);
}

export const pornhub = {
  id: SITE, name: 'Pornhub', displayName: 'Pornhub',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${BASE}/video?o=ht` : `${BASE}/video?o=ht&page=${page}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0, _httpStatus: status, _raw: body };
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = page <= 1 ? `${BASE}/video/search?search=${enc}` : `${BASE}/video/search?search=${enc}&page=${page}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async categoryPage(category, cursor) {
    const cq = CAT_PATHS.pornhub[category];
    if (!cq) return { items: [], nextCursor: null, hasMore: false };
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${BASE}/video?${cq}` : `${BASE}/video?${cq}&page=${page}`;
    const { status, body } = await fetchText(url, { referer: BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const { body } = await fetchText(card.pageUrl, { referer: BASE });
    const stream = parseWatch(body);
    if (!stream) throw new Error('No playable stream for ' + card.id);
    return stream;
  },
};
