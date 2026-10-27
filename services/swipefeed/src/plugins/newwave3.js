/**
 * 2026-10-18 第三批：porn87
 * 来自 `Richy-pr1.txt`（Porn87 去广告 + 悬浮下载）。
 *
 * 脚本里写明了取流规则：
 *   封面 `cdn-N.porn87.com/media/image_1/<hash>_N.jpg` → 正片 `cdn-N.porn87.com/media/video_1/<hash>.mp4/index.m3u8`（无加密 VOD）
 * 所以列表页就够用：卡片 `<a href="/main/html?id=<id>">` + `<img class="video_thumbnail" src="…image_1/<hash>_N.jpg">`，
 * resolve 直接由 hash 拼 m3u8，不再请求详情页（实测 206 + #EXTM3U）。
 */
import { fetchText } from '../util/http.js';

const BASE = 'https://porn87.com';
const UA_REF = {
  'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  'accept-language': 'zh-CN,zh;q=0.9',
};

function decode(s) {
  return String(s || '')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ').trim();
}

function cards(html) {
  const out = [];
  const seen = new Set();
  const chunks = String(html).split(/<a href="\/main\/html\?id=/).slice(1);
  for (const raw of chunks) {
    const id = (/^(\d+)/.exec(raw) || [])[1];
    if (!id || seen.has(id)) continue;
    const seg = raw.slice(0, 1600);
    const thumb = (/<img[^>]+class="video_thumbnail"[^>]+src="([^"]+)"/.exec(seg) || /<img[^>]+src="(https:\/\/cdn-\d+\.porn87\.com\/media\/image_1\/[^"]+)"/.exec(seg) || [])[1] || '';
    const hash = (thumb.match(/image_1\/([a-z0-9]+)_\d+\.jpg/i) || [])[1] || '';
    const title = decode((/<span[^>]*>\s*([^<]{4,200})<\/span>/.exec(seg.slice(seg.indexOf('video_time'))) || [])[1] || '');
    const time = (/(\d{1,2}:\d{2}(?::\d{2})?)/.exec((/<div class="video_time">[\s\S]{0,60}?(\d{1,2}:\d{2}(?::\d{2})?)/.exec(seg) || [])[1] || '') || [])[1];
    if (!hash && !title) continue;
    seen.add(id);
    out.push({
      site: 'porn87',
      id,
      title: title || `Porn87 ${id}`,
      cover: thumb ? thumb.replace(/^http:/, 'https:') : null,
      duration: time || null,
      pageUrl: `${BASE}/main/html?id=${id}`,
      aspect: 'horizontal',
      extra: { hash, cdn: thumb ? new URL(thumb).origin : 'https://cdn-1.porn87.com' },
    });
  }
  return out;
}

export const porn87 = {
  id: 'porn87',
  name: 'Porn87',
  displayName: 'Porn87',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const urls = page <= 1 ? [`${BASE}/`] : [`${BASE}/latest/?page=${page}`, `${BASE}/?page=${page}`];
    for (const url of urls) {
      try {
        const { status, body } = await fetchText(url, { referer: BASE + '/', headers: UA_REF });
        if (status >= 400) continue;
        const items = cards(body);
        if (items.length) return { items, nextCursor: { page: page + 1 }, hasMore: true };
      } catch { /* next */ }
    }
    return { items: [], nextCursor: null, hasMore: false };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${BASE}/search?q=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    try {
      const { status, body } = await fetchText(url, { referer: BASE + '/', headers: UA_REF });
      if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
      const items = cards(body);
      return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
    } catch { return { items: [], nextCursor: null, hasMore: false }; }
  },
  async resolve(card) {
    const extra = card.extra || {};
    let hash = extra.hash || '';
    let cdn = extra.cdn || 'https://cdn-1.porn87.com';
    if (!hash) {
      const { body } = await fetchText(card.pageUrl || `${BASE}/main/html?id=${card.id}`, { referer: BASE + '/', headers: UA_REF });
      const thumb = (body.match(/https?:\/\/cdn-\d+\.porn87\.com\/media\/image_1\/[^"'\s]+/i) || [])[0] || '';
      hash = (thumb.match(/image_1\/([a-z0-9]+)_\d+\.jpg/i) || [])[1] || '';
      if (thumb) cdn = new URL(thumb).origin;
    }
    if (!hash) throw new Error('porn87 未取到影片 hash');
    const url = `${cdn}/media/video_1/${hash}.mp4/index.m3u8`;
    return { url, type: 'hls', qualities: [{ label: '自动', url }], referer: card.pageUrl || BASE + '/' };
  },
};
