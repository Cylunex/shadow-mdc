import { fetchText, unescape, firstMatch, streamFromMap, collectHrefCards, looseMp4Hls, absUrl } from '../util/http.js';
import { cached } from '../util/list-cache.js';
import { CAT_PATHS } from './categories.js';

/**
 * Build a simple listing+resolve plugin from config.
 */
export function makePlugin(cfg) {
  const {
    id, name, base,
    listUrl, searchUrl, categoryUrl,
    hrefRe, idGroup = 2, pathGroup = 1, titleGroup = null,
    watchParsers = [],
    pageStart = 1,
  } = cfg;

  /** 列表 HTML → 卡片（导出给分类模板兜底复用，保证解析口径一致）。 */
  function parseListing(html) {
    return collectHrefCards(html, {
      site: id, base, hrefRe, idGroup, pathGroup, titleGroup,
    });
  }

  async function listFrom(urlBuilder, cursor) {
    const page = cursor?.page ?? pageStart;
    const url = urlBuilder(page);
    // 列表页 TTL 缓存 + 单飞：同页并发只打一次上游，回退/切站不再重复抓
    const { status, body } = await cached(`list:${url}`, () => fetchText(url, { referer: base }));
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseListing(body);
    const nextPage = page + 1;
    return {
      items,
      nextCursor: items.length ? { page: nextPage } : null,
      hasMore: items.length > 0,
      _httpStatus: status,
      _raw: body,
    };
  }

  return {
    id, name, displayName: name,
    base,
    parseListing,
    categoryTemplate: cfg.categoryTemplate || null,
    // 供分类动态探测使用：用哨兵 slug 调一次 categoryUrl，反推出该站的分类 URL 形状（去 origin）
    categoryPattern: (() => {
      if (!categoryUrl) return null;
      try {
        const sample = String(categoryUrl('__slug__', 1) || '');
        if (!sample.includes('__slug__')) return null;
        let rel = sample;
        if (/^https?:\/\//i.test(sample)) {
          try { rel = new URL(sample).pathname + (new URL(sample).search || ''); } catch { /* keep */ }
        }
        if (!rel.includes('__slug__')) return null;
        const at = rel.indexOf('__slug__');
        const withPlaceholder = rel.slice(0, at) + '{slug}' + rel.slice(at + 8);
        return withPlaceholder.startsWith('/') ? withPlaceholder : `/${withPlaceholder}`;
      } catch {
        return null;
      }
    })(),
    async nextPage(cursor) {
      return listFrom((page) => listUrl(page), cursor);
    },
    async search(q, cursor) {
      if (!searchUrl) return { items: [], nextCursor: null, hasMore: false };
      return listFrom((page) => searchUrl(q.trim(), page), cursor);
    },
    async categoryPage(category, cursor) {
      if (!categoryUrl) return { items: [], nextCursor: null, hasMore: false };
      const path = (CAT_PATHS[id] || {})[category];
      if (!path && !categoryUrl(category, pageStart)) {
        // still try categoryUrl with slug=category
      }
      return listFrom((page) => categoryUrl(category, page), cursor);
    },
    async resolve(card) {
      // 站点自带解析（需要跑 JS / 走 embed 的站）优先，cfg.resolve 覆盖默认的静态抓取
      if (typeof cfg.resolve === 'function') return cfg.resolve(card);
      const { body } = await fetchText(card.pageUrl, { referer: base });
      for (const parser of watchParsers) {
        const s = parser(body, card);
        if (s) return s;
      }
      const loose = looseMp4Hls(body);
      if (loose) return loose;
      throw new Error('No playable stream for ' + card.id);
    },
  };
}

export function mediaDefinitionsParser(html) {
  const raw = firstMatch(/(?:mediaDefinitions|media_definitions)\s*[:=]\s*(\[[\s\S]*?\])\s*[,;]/i, html);
  if (!raw) return null;
  try {
    const arr = JSON.parse(unescape(raw));
    const q = {};
    for (const o of arr) {
      const url = unescape(o.videoUrl || o.video_url || '');
      if (!url.startsWith('http')) continue;
      let quality = o.quality;
      if (Array.isArray(quality)) quality = quality[0];
      q[String(quality || 'default')] = url;
    }
    return streamFromMap(q);
  } catch {
    return null;
  }
}

export function xvideosStyleParser(html) {
  const q = {};
  const hls = firstMatch(/html5player\.setVideoHLS\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  const high = firstMatch(/html5player\.setVideoUrlHigh\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  const low = firstMatch(/html5player\.setVideoUrlLow\(\s*['"]([^'"]+)['"]\s*\)/i, html);
  if (hls) q.hls = unescape(hls);
  if (high) q.high = unescape(high);
  if (low) q.low = unescape(low);
  return streamFromMap(q);
}

export function encodingsParser(html) {
  const raw = firstMatch(/encodings\s*[:=]\s*(\[[\s\S]*?\])\s*;/i, html);
  if (!raw) return null;
  try {
    const arr = JSON.parse(raw);
    const q = {};
    for (const o of arr) {
      let url = o.filename || o.src || o.url || '';
      if (url.startsWith('//')) url = 'https:' + url;
      if (!url.startsWith('http')) continue;
      q[String(o.quality || o.label || 'default')] = unescape(url);
    }
    return streamFromMap(q);
  } catch {
    return null;
  }
}

export function streamDataParser(html) {
  const raw = firstMatch(/var\s+stream_data\s*=\s*(\{.*?\});/is, html);
  if (!raw) return null;
  try {
    const obj = JSON.parse(raw);
    const q = {};
    for (const [key, value] of Object.entries(obj)) {
      if (Array.isArray(value)) {
        value.forEach((u, i) => {
          if (typeof u === 'string' && u.startsWith('http')) q[`${key}_${i}`] = unescape(u);
        });
      } else if (typeof value === 'string' && value.startsWith('http')) {
        q[key] = unescape(value);
      }
    }
    return streamFromMap(q);
  } catch {
    return null;
  }
}

/** KVS / Kernel Video Sharing flashvars + <source> tags (get_file MP4s). */
export function kvsFlashvarsParser(html) {
  const q = {};
  const keys = ['video_url', 'video_alt_url', 'video_alt_url2', 'video_alt_url3', 'video_alt_url4'];
  for (const key of keys) {
    const re = new RegExp(key + String.raw`\s*:\s*['"]([^'"]+)['"]`, 'i');
    let url = firstMatch(re, html);
    if (!url) continue;
    url = unescape(url).replace(/^function\/\d+\//, '');
    if (url.startsWith('//')) url = 'https:' + url;
    if (!url.startsWith('http')) continue;
    if (/\.mp4\.jpg|preview_|screenshot|timeline/i.test(url) && !/get_file/i.test(url)) continue;
    const label =
      firstMatch(new RegExp(key + String.raw`_text\s*:\s*['"]([^'"]+)['"]`, 'i'), html) || key;
    q[label] = url;
  }
  const sourceRe = /<source[^>]+src=["']([^"']+)["'][^>]*>/gi;
  let m;
  while ((m = sourceRe.exec(html)) !== null) {
    let url = unescape(m[1]);
    if (url.startsWith('//')) url = 'https:' + url;
    if (!url.startsWith('http')) continue;
    if (/\.mp4\.jpg|preview_/i.test(url)) continue;
    const label =
      firstMatch(/title=["']([^"']+)["']/i, m[0]) ||
      firstMatch(/label=["']([^"']+)["']/i, m[0]) ||
      'source';
    q[label] = url;
  }
  // contentUrl in JSON-LD
  const contentUrl = firstMatch(/"contentUrl"\s*:\s*"([^"]+)"/i, html);
  if (contentUrl) {
    let url = unescape(contentUrl);
    if (url.startsWith('http') && !/\.mp4\.jpg/i.test(url)) q.contentUrl = url;
  }
  return streamFromMap(q);
}

/** AnyPorn-style `const sources = { 360: 'url', ... }`. */
export function qualityMapParser(html) {
  const raw = firstMatch(/const\s+sources\s*=\s*(\{[\s\S]*?\})\s*;/i, html);
  if (!raw) return null;
  const q = {};
  const pairRe = /['"]?(\d{3,4}|default|auto)['"]?\s*:\s*['"](https?:[^'"]+)['"]/gi;
  let m;
  while ((m = pairRe.exec(raw)) !== null) {
    q[m[1]] = unescape(m[2]);
  }
  return streamFromMap(q);
}
