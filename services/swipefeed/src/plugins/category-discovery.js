/**
 * category-discovery.js — 站点分类的「动态解析」。
 *
 * 以前 /api/browse/:id 对没有自己 categories() 的源回落到写死的 CATEGORIES（6 个词），
 * 那 6 个词对多数站点其实是无效的（拼出来的 URL 直接 404）。现在按下面的顺序真去找：
 *
 *   1) 插件自带 `src.categories()`（海角节点、MediaCMS 分类、VideosHub 之类，最可靠）
 *   2) 插件的 `src.categoryPaths`（显式映射表，id → slug / 完整 URL）
 *   3) 站点首页 / 分类页里的链接抽取（同时反推该站的真实 URL 形状）
 *   4) 用候选 slug × 站点 URL 形状逐个请求校验（HEAD/GET，看到列表就算命中）
 *
 * 结果带 `source` 字段说明分类是怎么来的，前端可以直接显示真实分类；
 * 完全找不到时返回空数组（前端就不显示筛选条，而不是给一堆点了没用的标签）。
 */
import { fetchText } from '../util/http.js';
import { browserFetchEnabled, renderHtml } from '../util/browser-fetch.js';
import {
  CATEGORIES, SITE_BASE, SLUG_CANDIDATES, CATEGORY_URL_PATTERNS,
} from './categories.js';
import { categoryTemplateOf } from './registry.js';

const CACHE_TTL_MS = 30 * 60 * 1000;
const cache = new Map(); // siteId -> { at, value }

const MAX_CANDIDATES = 14;
const MAX_VALIDATE_REQUESTS = 30;
const CONCURRENCY = 8;
const PROBE_TIMEOUT = 6000;
const SLUG_PHASE_BUDGET_MS = 25000;
const MAX_CONSECUTIVE_FAILS = 8;

/* ─────────────── 工具 ─────────────── */

const abs = (raw, base) => {
  try {
    return new URL(raw, base).toString();
  } catch {
    return null;
  }
};

/** 分类页长什么样：有列表结构就算（不同站卡片 class 与链接形状千差万别）。 */
function looksLikeListingPage(html) {
  if (!html || html.length < 2000) return false;
  // 明确的错误页直接否掉（有的站错误页带一整套导航，长度和锚点数都不低）
  if (/<title>[^<]*(?:404|not found|error|出错了)[^<]*<\/title>/i.test(html)) return false;
  if (/(?:\.\.\.)?(?:页面不存在|找不到页面|no longer available)/i.test(html.slice(0, 3000))) return false;
  const videoLinks = (html.match(/href="[^"]*\/(?:video|view_video|watch|videos|v)\/?[^"]*"/gi) || []).length;
  if (videoLinks >= 8) return true;
  const cards = (html.match(/class="[^"]*(?:thumb|video-item|card|item|video)/gi) || []).length;
  if (cards >= 12) return true;
  // 兜底：分类页通常又长又密（锚点多、体积大），导航页达不到
  const anchors = (html.match(/<a\s/gi) || []).length;
  if (anchors >= 200 && html.length > 40000) return true;
  return false;
}

/** 链接文本 → 分类显示名（去掉站点拼在 slug 尾部的数字后缀，如 Arab-159 → Arab）。 */
function labelOf(slug) {
  return String(slug)
    .replace(/[_/]+/g, '-')
    .replace(/-\d{1,4}$/, '')
    .split('-')
    .filter(Boolean)
    .map((w) => (w.length <= 3 && /^\d/.test(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(' ');
}

async function probeUrl(url) {
  try {
    const { status, body } = await fetchText(url, { desktop: true, timeout: PROBE_TIMEOUT, retry: false });
    // 404/403 的“错误页”往往也又长又密，必须按状态码先筛掉
    if (status >= 400) return null;
    if (!looksLikeListingPage(body)) return null;
    return { status, len: body.length };
  } catch {
    return null;
  }
}

/** 限并发跑一批任务，返回成功结果；支持总时限与“连续失败就收手”。 */
async function probeBatch(items, task, {
  concurrency = CONCURRENCY, stopAfter = Infinity, deadline = 0, maxFails = MAX_CONSECUTIVE_FAILS,
} = {}) {
  const out = [];
  let cursor = 0;
  let stop = false;
  let fails = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (!stop) {
      if (deadline && Date.now() > deadline) { stop = true; return; }
      const idx = cursor++;
      if (idx >= items.length) return;
      const r = await task(items[idx]);
      if (r) {
        out.push(r);
        fails = 0;
        if (out.length >= stopAfter) stop = true;
      } else if (++fails >= maxFails) {
        stop = true; // 连续失败太多：这站大概不吃这个形状，别把时间耗完
      }
    }
  });
  await Promise.all(workers);
  return out;
}

/* ─────────────── 首页 / 分类页链接抽取 ─────────────── */

const INDEX_PATHS = [
  '/', '/categories/', '/categories', '/tags/', '/channels/', '/c/',
  // 某些站把分类索引放在单独路径，甚至返回 404 但页面里带链接（pornhub）
  '/video/categories', '/video/categories/', '/categories/all', '/category/',
  // 各站实测出来的索引页（tube8 用 .html 后缀，porndoe 只有 /categories）
  '/categories.html', '/porn-categories', '/tags',
];

/**
 * 分类链接抽取。slug 可能不止一段（porndoe 是 /category/32/hd-videos），
 * 这时把「去掉末段」当成形状的一部分，保证 {slug} 替换后地址仍然成立。
 */
function extractCategoryLinks(html, base, { directory = false } = {}) {
  const found = new Map(); // slug -> { slug, url, label }
  const patterns = directory
    ? [/href="([^"]*\/(?:categories|category|tags|channels)\/([^"?#]+))"/gi]
    : [
      /href="([^"]*\/categories\/[^"?#]*?)"/gi,
      /href="([^"]*\/category\/[^"?#]*?)"/gi,
      /href="([^"]*\/tags\/[^"?#]*?)"/gi,
      /href="([^"]*\/c\/[^"?#]*?)"/gi,
      /href="([^"]*\/channels\/[^"?#]*?)"/gi,
    ];
  for (const re of patterns) {
    for (const m of html.matchAll(re)) {
      const url = abs(m[1], base);
      if (!url) continue;
      const tail = /(?:categories|category|tags|c|channels)\/([^/?#]+(?:\/[^/?#]+)?)/i.exec(url)?.[1];
      if (!tail || tail.length < 2 || tail.length > 48) continue;
      const slug = tail.split('/').filter(Boolean).slice(-1)[0];
      if (!slug || /^(all|index|popular|new|top|page|\d+)$/i.test(slug)) continue;
      if (found.has(slug)) continue;
      // 带前缀段的地址：形状里保留前缀，slug 只替换最后一段
      const prefix = tail.includes('/') ? tail.slice(0, tail.length - slug.length) : '';
      found.set(slug, {
        id: slug,
        url,
        displayName: labelOf(slug),
        _prefix: prefix, // 例如 '32/'
      });
    }
  }
  return [...found.values()];
}

/**
 * 目录页模式：站点把分类索引放在 /categories/ 这类“目录页”上（页面里没有视频卡片，
 * 只有一串分类链接）。porntube / txxx 就是这样。
 */
async function discoverViaDirectory(base, siteId, src) {
  for (const dir of ['/categories/', '/tags/', '/channels/', '/c/']) {
    const url = abs(dir, base);
    if (!url) continue;
    let html = '';
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await fetchText(url, { desktop: true, timeout: PROBE_TIMEOUT, retry: false });
      html = r.body || '';
    } catch {
      continue;
    }
    const links = extractCategoryLinks(html, base, { directory: true });
    if (links.length >= 10) {
      const pattern = derivePattern(links[0].url, links[0].id, links[0]._prefix || '') || patternOf(src, siteId);
      // 校验 2 个：确认这些链接真能出视频
      const probe = await probeBatch(links.slice(0, 4), async (l) => {
        const u = abs(l.url, base);
        const ok = u ? await probeUrl(u) : null;
        return ok ? l.id : null;
      }, { concurrency: 2, stopAfter: 1, maxFails: 4 });
      if (probe.length) {
        return {
          site: siteId,
          categories: links.slice(0, 60).map((l) => ({ id: l.id, displayName: l.displayName })),
          source: 'category-index',
          pattern,
          note: `${links.length} 个来自目录页 ${dir}`,
        };
      }
    }
  }
  return null;
}

/** 从完整 URL 反推该站分类 URL 形状；带前缀段的（/category/32/hd-videos）会保留前缀。 */
function derivePattern(sampleUrl, slug, prefix = '') {
  try {
    const u = new URL(sampleUrl);
    const path = decodeURIComponent(u.pathname);
    const needle = prefix ? prefix + slug : slug;
    const at = path.indexOf(needle);
    if (at < 0) return null;
    const derived = path.slice(0, at) + prefix + '{slug}' + path.slice(at + needle.length);
    return u.search ? `${derived}${u.search}` : derived;
  } catch {
    return null;
  }
}

function patternOf(src, siteId) {
  // 源的“自家形状”最可信：探测与过滤都用它，探测出来的 slug 必然能出列表
  try {
    const fromRegistry = categoryTemplateOf(siteId);
    if (fromRegistry) return fromRegistry;
  } catch { /* registry 尚未就绪时继续往下 */ }
  if (src && typeof src.categoryPattern === 'string' && src.categoryPattern) return src.categoryPattern;
  if (src && typeof src.categoryTemplate === 'string' && src.categoryTemplate) return src.categoryTemplate;
  if (src && typeof src.categoryPaths === 'object' && src.categoryPaths) {
    const vals = Object.values(src.categoryPaths).filter((v) => typeof v === 'string');
    if (vals.length) {
      // categoryPaths 取值可能是纯 slug、带斜杠的路径、或 query（c=7）
      const withSlash = vals.find((v) => /\//.test(v));
      const sample = withSlash || vals[0];
      if (/^c=\d+$/.test(sample)) return '/video?c={slug}';
      return sample.includes('/') ? `/${sample.split('/').slice(0, -1).join('/')}/{slug}/` : `/${sample}`;
    }
  }
  return CATEGORY_URL_PATTERNS[siteId] || null;
}

/* ─────────────── 主入口 ─────────────── */

/**
 * @param {string} siteId
 * @param {object|null} src 源插件（有 categories() / categoryPaths 时优先用）
 * @param {{ force?: boolean }} [opts]
 * @returns {Promise<{ site: string, categories: Array<{id,displayName,hint?}>, source: string, pattern: string|null, note?: string }>}
 */
export async function discoverCategories(siteId, src = null, { force = false } = {}) {
  const cached = cache.get(siteId);
  if (!force && cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.value;

  let result = null;

  // 1) 插件自带
  if (src && typeof src.categories === 'function') {
    try {
      const list = await src.categories();
      if (Array.isArray(list) && list.length) {
        result = {
          site: siteId,
          categories: list.map((c) => ({
            id: c.id ?? c.uid ?? c.title,
            displayName: c.displayName || c.title || c.name || String(c.id ?? c.uid),
            hint: c.count != null ? `${c.count}` : undefined,
          })),
          source: 'plugin-api',
          pattern: null,
        };
      }
    } catch { /* 落到下面的探测 */ }
  }

  const base = SITE_BASE[siteId] || (src && src.base) || null;

  // 2) 首页 / 分类索引页链接抽取
  if (!result && base) {
    for (const p of INDEX_PATHS) {
      const url = abs(p, base);
      if (!url) continue;
      let html = '';
      try {
        // eslint-disable-next-line no-await-in-loop
        const r = await fetchText(url, { desktop: true, timeout: PROBE_TIMEOUT, retry: false });
        // 注意：索引页有时是 404 状态但正文里就有全套分类链接（pornhub 就是），
        // 所以这里不看状态码，只看抽出来的链接够不够。
        html = r.body || '';
      } catch {
        continue;
      }
      if (!html) continue;
      const links = extractCategoryLinks(html, base);
      if (links.length >= 4) {
        const pattern = derivePattern(links[0].url, links[0].id, links[0]._prefix || '') || patternOf(src, siteId);
        const probe = await probeBatch(links.slice(0, 3), async (l) => {
          const u = abs(l.url, base);
          const ok = u ? await probeUrl(u) : null;
          return ok ? l.id : null;
        }, { concurrency: 3, stopAfter: 1, maxFails: 3 });
        if (probe.length) {
          result = {
            site: siteId,
            categories: links.slice(0, 60).map((l) => ({ id: l.id, displayName: l.displayName })),
            source: 'homepage-links',
            pattern,
            note: `${links.length} 个来自 ${new URL(url).pathname} 的链接`,
          };
          break;
        }
      }
    }
  }

  // 2b) 目录页（只有分类链接、没有视频卡片的索引页）
  if (!result && base) {
    result = await discoverViaDirectory(base, siteId, src);
  }

  // 2c) 浏览器兜底：分类是前端 JS 渲染出来的站点（porndoe / empflix / tube8 …）
  if (!result && base && browserFetchEnabled()) {
    for (const p of ['/', '/categories', '/categories.html']) {
      const url = abs(p, base);
      if (!url) continue;
      // eslint-disable-next-line no-await-in-loop
      const html = await renderHtml(url);
      if (!html) continue;
      const links = extractCategoryLinks(html, base);
      const dirLinks = extractCategoryLinks(html, base, { directory: true });
      const pool = links.length >= dirLinks.length ? links : dirLinks;
      if (pool.length >= 4) {
        const pattern = derivePattern(pool[0].url, pool[0].id, pool[0]._prefix || '') || patternOf(src, siteId);
        result = {
          site: siteId,
          categories: pool.slice(0, 60).map((l) => ({ id: l.id, displayName: l.displayName })),
          source: 'browser-render',
          pattern,
          note: `headless 浏览器渲染 ${new URL(url).pathname} 得到 ${pool.length} 个分类`,
        };
        break;
      }
    }
  }

  // 3) 候选 slug × URL 形状
  if (!result) {
    const patterns = [];
    const primary = patternOf(src, siteId);
    if (primary) patterns.push(primary);
    // 多重形状兜底：不同站把分类放在不同前缀下
    const generic = ['/categories/{slug}/', '/categories/{slug}', '/category/{slug}/', '/tags/{slug}/', '/tags/{slug}', '/channels/{slug}/', '/c/{slug}/'];
    for (const g of generic) if (!patterns.includes(g)) patterns.push(g);
    // categoryUrl 函数型站点（factory）无法直接读取形状，用通用形状兜底
    const candidates = [...new Set([
      ...SLUG_CANDIDATES.slice(0, MAX_CANDIDATES),
      ...CATEGORIES.map((c) => c.id),
    ])];

    let requests = 0;
    const deadline = Date.now() + SLUG_PHASE_BUDGET_MS;
    for (const pattern of patterns) {
      if (requests >= MAX_VALIDATE_REQUESTS || Date.now() > deadline) break;
      const urls = candidates.map((slug) => {
        const path = pattern.replace('{slug}', slug);
        return { slug, url: abs(path.startsWith('/') ? path : `/${path}`, base) };
      }).filter((x) => x.url);
      const hits = await probeBatch(urls, async (item) => {
        if (requests >= MAX_VALIDATE_REQUESTS || Date.now() > deadline) return null;
        requests += 1;
        const ok = await probeUrl(item.url);
        return ok ? { slug: item.slug, url: item.url } : null;
      }, { concurrency: CONCURRENCY, stopAfter: 6, deadline });
      if (hits.length >= 2) {
        result = {
          site: siteId,
          categories: hits.map((h) => ({ id: h.slug, displayName: labelOf(h.slug) })),
          source: 'slug-probe',
          pattern,
          note: `用形状 ${pattern} 校验通过 ${hits.length} 个`,
        };
        break;
      }
    }
  }

  const value = result || {
    site: siteId,
    categories: [],
    source: 'none',
    pattern: null,
    note: '没找到可用的分类入口（站点未暴露分类链接，候选 slug 也没命中）',
  };
  cache.set(siteId, { at: Date.now(), value });
  return value;
}

/** 只读缓存，不触发探测（给 /api/browse/:id 的快速路径用）。 */
export function peekCategories(siteId) {
  const hit = cache.get(siteId);
  return hit ? hit.value : null;
}

export function clearCategoryCache(siteId = null) {
  if (siteId) cache.delete(siteId);
  else cache.clear();
}
