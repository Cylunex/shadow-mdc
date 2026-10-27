/**
 * 2026-10-18 从 tool/ 新脚本里挑出来、服务端可直取的两个站：
 *
 *  · cherrygasp —— 来自 `Richy-ou.txt`（CherryGasp 去广告·隐私·下载）
 *      KVS 主题：列表是 `div.item > a[href=/video/<slug>/]`（title 属性即标题），
 *      封面 `data-original`，播放地址在详情页 `video_url` / `video_alt_url`（480p / 720p MP4）。
 *  · jiuse —— 来自 `Richy-9s.txt`（九色 jiuse.tv 去广告 + 悬浮下载）
 *      列表 `a[href=/video/view/<id>]` + 背景图 `url(//host/thumb/<id>.jpg)` + `<small class="layer">` 时长，
 *      详情页里带 `https://cdn2.jiuse3.cloud/hls/<n>/index.m3u8?t=…&m=…`，清单明文、分片直接可用。
 *
 * 两个站都不需要登录、不需要浏览器，全部 HTTP 直取。
 */
import { fetchText } from '../util/http.js';

const UA_REF = { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36' };

function decode(s) {
  return String(s || '')
    .replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

function dur(text) {
  const m = /(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(String(text || ''));
  if (!m) return null;
  return m[3] ? `${m[1]}:${m[2]}:${m[3]}` : `${m[1]}:${m[2]}`;
}

/* ─────────────────────────── CherryGasp ─────────────────────────── */

const CG_BASE = 'https://cherrygasp.com';

function cgCards(html) {
  const out = [];
  const seen = new Set();
  const itemRe = /<div class="item[^"]*">([\s\S]{0,2200}?)<\/div>\s*<\/div>/g;
  const blocks = [...String(html).matchAll(itemRe)].map((m) => m[1]);
  const pool = blocks.length ? blocks : [String(html)];
  for (const block of pool) {
    const a = /<a href="(https?:\/\/cherrygasp\.com\/video\/[^"]+\/)"[^>]*title="([^"]*)"/i.exec(block)
      || /<a href="(https?:\/\/cherrygasp\.com\/video\/[^"]+\/)"/i.exec(block);
    if (!a) continue;
    const pageUrl = a[1];
    if (seen.has(pageUrl)) continue;
    seen.add(pageUrl);
    const title = decode(a[2] || (/title="([^"]+)"/.exec(block) || [])[1] || pageUrl.split('/video/')[1]?.replace(/\/$/, '') || '');
    const cover = (/data-original="([^"]+)"/.exec(block) || /data-webp="([^"]+)"/.exec(block) || /src="(https?:\/\/[^"]+\.jpg)"/.exec(block) || [])[1] || null;
    const time = dur((/<span class="duration">([^<]+)</.exec(block) || /<small class="layer">([^<]+)</.exec(block) || [])[1]);
    out.push({
      site: 'cherrygasp',
      id: pageUrl.split('/video/')[1].replace(/\/$/, ''),
      title,
      cover: cover ? decode(cover) : null,
      duration: time,
      pageUrl,
      aspect: 'horizontal',
      extra: {},
    });
  }
  return out;
}

export const cherrygasp = {
  id: 'cherrygasp',
  name: 'CherryGasp',
  displayName: 'CherryGasp',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = page <= 1 ? `${CG_BASE}/` : `${CG_BASE}/videos/?page=${page}`;
    const { status, body } = await fetchText(url, { referer: CG_BASE + '/', headers: UA_REF });
    if (status >= 400) throw Object.assign(new Error(`cherrygasp HTTP ${status}`), { status });
    const items = cgCards(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${CG_BASE}/search/?q=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    const { status, body } = await fetchText(url, { referer: CG_BASE + '/', headers: UA_REF });
    if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
    const items = cgCards(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${CG_BASE}/video/${card.id}/`;
    const { status, body } = await fetchText(pageUrl, { referer: CG_BASE + '/', headers: UA_REF });
    if (status >= 400) throw new Error(`cherrygasp 详情页 HTTP ${status}`);
    const pick = (k) => (body.match(new RegExp(`${k}\\s*:\\s*'([^']+)'`)) || [])[1] || '';
    const sd = pick('video_url');
    const hd = pick('video_alt_url');
    const best = hd || sd;
    if (!best) throw new Error('cherrygasp 未找到 video_url（页面结构可能变了）');
    const qualities = [];
    if (hd) qualities.push({ label: '720p', url: hd });
    if (sd) qualities.push({ label: '480p', url: sd });
    return { url: best, type: 'mp4', qualities, referer: pageUrl };
  },
};

/* ─────────────────────────── 九色 jiuse ─────────────────────────── */

const JS_BASE = 'https://jiuse.tv';

function jsCards(html) {
  const out = [];
  const seen = new Set();
  const text = String(html);
  // 按卡片块切分（每块一只视频）：<div class="colVideoList"> … </div>
  const blocks = text.split(/<div class="colVideoList">/).slice(1);
  const chunks = blocks.length ? blocks : [text];
  for (const block of chunks) {
    const seg = block.slice(0, 2500);
    const id = (/href="\/video\/view\/([0-9a-z]{5,})"/i.exec(seg) || [])[1];
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const thumb = (/url\(\s*'([^']+)'\s*\)/.exec(seg) || /url\("\s*([^"]+)"\s*\)/.exec(seg) || [])[1] || '';
    const cover = thumb ? (thumb.startsWith('//') ? `https:${thumb}` : thumb) : null;
    const time = dur((/<small class="layer">\s*([^<]+?)\s*<\/small>/.exec(seg) || [])[1]);
    const title = decode((/<a[^>]*class="title[^"]*"[^>]*>([\s\S]{0,200}?)<\/a>/.exec(seg) || [])[1] || '');
    if (!title && !cover) continue;
    out.push({
      site: 'jiuse',
      id,
      title: title || `jiuse ${id}`,
      cover,
      duration: time,
      pageUrl: `${JS_BASE}/video/view/${id}`,
      aspect: 'horizontal',
      extra: {},
    });
  }
  return out;
}

export const jiuse = {
  id: 'jiuse',
  name: 'Jiuse',
  displayName: '九色（jiuse.tv）',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${JS_BASE}/index.php/video/index.html?page=${page}`;
    const { status, body } = await fetchText(url, { referer: JS_BASE + '/', headers: UA_REF });
    if (status >= 400) throw Object.assign(new Error(`jiuse HTTP ${status}`), { status });
    const items = jsCards(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${JS_BASE}/index.php/video/search.html?keyword=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    const { status, body } = await fetchText(url, { referer: JS_BASE + '/', headers: UA_REF });
    if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
    const items = jsCards(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${JS_BASE}/video/view/${card.id}`;
    const { status, body } = await fetchText(pageUrl, { referer: JS_BASE + '/', headers: UA_REF });
    if (status >= 400) throw new Error(`jiuse 详情页 HTTP ${status}`);
    const raw = (body.match(/https?:\/\/[^"'\s<>\\]+\.m3u8[^"'\s<>\\]*/i) || [])[0];
    if (!raw) throw new Error('jiuse 未找到 m3u8（页面结构可能变了）');
    const url = raw.replace(/&amp;/g, '&');
    return { url, type: 'hls', qualities: [{ label: '自动', url }], referer: pageUrl };
  },
};
