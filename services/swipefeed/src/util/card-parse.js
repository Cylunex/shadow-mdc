/**
 * 列表卡片元数据提取（title / cover / duration / author）。
 *
 * 背景：原 `collectHrefCards` 只按 href 抠 id，卡片一律 `Video <id>` + 无封面，
 * Android 端刷流/瀑布流因此出现整屏黑底或纯文字。这里改成「以命中 href 为中心、
 * 取其所在卡片块」的局部解析：只在块内找属性/图片/时长，绝不跨卡片污染。
 *
 * 设计约束（重要）：
 *   · 纯正则 + 切片，零新依赖，Kotlin 侧可 1:1 移植（TubeCardParser）；
 *   · 单遍扫描、块长度上限，避免大 HTML 上的回溯爆炸；
 *   · 任何字段解析失败都只回落 null，绝不抛异常影响列表。
 */

const ENTITIES = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'",
  '&#x27;': "'", '&apos;': "'", '&nbsp;': ' ', '&hellip;': '…',
  '&mdash;': '—', '&ndash;': '–', '&middot;': '·', '&times;': '×',
};

/** HTML 实体解码（只处理常见实体 + 数字实体）。 */
export function decodeEntities(input) {
  if (!input) return '';
  return String(input)
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => safeChar(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => safeChar(parseInt(dec, 10)))
    .replace(/&[a-z]+;/gi, (m) => (ENTITIES[m.toLowerCase()] != null ? ENTITIES[m.toLowerCase()] : m));
}

function safeChar(code) {
  if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) return '';
  try {
    return String.fromCodePoint(code);
  } catch {
    return '';
  }
}

/** 去标签 + 解实体 + 压空白。 */
export function cleanText(input) {
  if (!input) return null;
  const t = decodeEntities(String(input).replace(/<[^>]*>/g, ' '))
    .replace(/\s+/g, ' ')
    .trim();
  return t || null;
}

/** 读一个开始标签里的属性值（只支持双/单引号与裸值）。 */
export function attrOf(tag, name) {
  if (!tag) return null;
  const re = new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'>]+))`, 'i');
  const m = re.exec(tag);
  if (!m) return null;
  const v = cleanText(m[1] ?? m[2] ?? m[3]);
  return v || null;
}

const PLACEHOLDER_IMG = /(?:^data:)|(?:blank|spacer|placeholder|transparent|1x1|lazy)\.(?:gif|png|svg)|(?:\/pixel\.gif)/i;

/** 图片 URL 归一化：跳过懒加载占位图，补全协议/域名。 */
export function normalizeImage(raw, base) {
  if (!raw) return null;
  let url = String(raw).trim();
  // 个别模板会把地址拼成 `https://site/&quot;https://cdn/x.jpg&quot;`（TXXX 的封面就是这样），
  // 这时取里层真正的图片地址，别让外层站点当域名。
  if (/&quot;|%22|"/.test(url)) {
    const inner = url.match(/https?:\/\/[^\s"'&]+?\.(?:jpe?g|png|webp|gif|avif)[^\s"']*/i);
    if (inner) url = inner[0];
  }
  url = url.replace(/^['"]|['"]$/g, '').replace(/(?:&quot;|&#34;|&amp;)+$/g, '');
  if (!url || PLACEHOLDER_IMG.test(url)) return null;
  if (/^data:/i.test(url)) return null;
  if (url.startsWith('//')) url = 'https:' + url;
  else if (url.startsWith('/')) url = String(base || '').replace(/\/$/, '') + url;
  else if (!/^https?:\/\//i.test(url)) {
    if (/^[\w.-]+\.[a-z]{2,}\//i.test(url)) url = 'https://' + url;
    else if (base) url = String(base).replace(/\/$/, '') + '/' + url.replace(/^\.?\//, '');
  }
  return /^https?:\/\//i.test(url) ? url : null;
}

/** 秒数 / "12:34" / "PT12M34S" → 统一 `M:SS` 或 `H:MM:SS`。 */
export function normalizeDuration(raw) {
  if (raw == null) return null;
  const s = String(raw).trim();
  if (!s) return null;

  const iso = /^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/i.exec(s);
  if (iso && (iso[1] || iso[2] || iso[3])) {
    return formatHms(Number(iso[1] || 0), Number(iso[2] || 0), Number(iso[3] || 0));
  }
  const colon = /^(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?$/.exec(s);
  if (colon) {
    if (colon[3] != null) {
      return formatHms(Number(colon[1]), Number(colon[2]), Number(colon[3]));
    }
    return formatHms(0, Number(colon[1]), Number(colon[2]));
  }
  if (/^\d{1,5}$/.test(s)) {
    const total = Number(s);
    if (total > 0 && total < 6 * 3600) return formatHms(0, Math.floor(total / 60), total % 60);
  }
  const cn = /^(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?$/.exec(s);
  if (cn && (cn[1] || cn[2] || cn[3])) {
    return formatHms(Number(cn[1] || 0), Number(cn[2] || 0), Number(cn[3] || 0));
  }
  return null;
}

function formatHms(h, m, s) {
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  if (m > 0 || s > 0) return `${m}:${pad(s)}`;
  return null;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

/** 标题清洗：去掉站点尾巴、播放量尾巴之类噪声。 */
export function cleanTitle(raw) {
  let t = cleanText(raw);
  if (!t) return null;
  t = t.replace(/\s*[-|·]\s*(?:watch|video|免费|在线观看|高清)\s*$/i, '').trim();
  if (t.length > 200) t = t.slice(0, 200).trim();
  if (!t) return null;
  if (/^(?:video|image|thumbnail|thumb|photo|picture)$/i.test(t)) return null;
  return t;
}

const BLOCK_SEARCH_BACK = 1200;   // 往回找卡片块起点的上限
const BLOCK_MAX = 4000;           // 单个卡片块的最大长度
const NEXT_ANCHOR = /<a\b/gi;

/**
 * 取命中 href 所在的卡片块范围：从所在 `<a ...>` 起点，到下一个 `<a` 之前。
 * 找不到 `<a` 时退化为命中点前后窗口，保证总有可解析文本。
 */
export function cardBlockRange(html, start, end) {
  const from = Math.max(0, start - BLOCK_SEARCH_BACK);
  const aAt = html.lastIndexOf('<a', start);
  const anchorStart = aAt >= from ? aAt : Math.max(0, start - 200);
  const searchFrom = Math.max(anchorStart + 2, end);
  NEXT_ANCHOR.lastIndex = searchFrom;
  const next = NEXT_ANCHOR.exec(html);
  let blockEnd = next ? next.index : html.length;
  if (blockEnd - anchorStart > BLOCK_MAX) blockEnd = anchorStart + BLOCK_MAX;
  if (blockEnd <= anchorStart) blockEnd = Math.min(html.length, end + 200);
  return { from: anchorStart, to: blockEnd };
}

export function cardBlockAt(html, start, end) {
  const { from, to } = cardBlockRange(html, start, end);
  return html.slice(from, to);
}

/** 卡片块之后的一小段尾巴：作者/频道链接常常挂在块外（下一张卡之前）。 */
export function cardTailAt(html, blockEnd, len = 320) {
  if (!html || blockEnd >= html.length) return '';
  return html.slice(blockEnd, Math.min(html.length, blockEnd + len));
}

const IMG_TAG = /<img\b[^>]*>/gi;
const SOURCE_TAG = /<source\b[^>]*>/gi;
const VIDEO_TAG = /<video\b[^>]*>/gi;
const BG_IMAGE = /background(?:-image)?\s*:\s*url\(\s*['"]?([^'")]+)['"]?\s*\)/i;
const DURATION_TEXT = /(?:duration|length|time|runtime)[^>]{0,40}>\s*([^<]{2,14})</i;
const LOOSE_TIME = />\s*(\d{1,3}:\d{1,2}(?::\d{1,2})?)\s*</;
const AUTHOR_LINK = /href=["'](?:https?:\/\/(?:www\.)?[^"']+)?\/(?:models?|pornstars?|channels?|profile|u)\/([^"'/?#]+)/i;

function firstGroup(re, text) {
  const m = re.exec(text);
  return m && m[1] ? cleanText(m[1]) : null;
}

/** 从卡片块里抽封面。优先级：懒加载真图 > 常规 src > <video poster> > CSS 背景。 */
export function coverFromBlock(block, base) {
  if (!block) return null;
  IMG_TAG.lastIndex = 0;
  let tag;
  let fallback = null;
  while ((tag = IMG_TAG.exec(block)) !== null) {
    const lazyload = ['data-src', 'data-original', 'data-lazy-src', 'data-echo', 'data-thumb', 'data-cover', 'data-poster']
      .map((a) => normalizeImage(attrOf(tag[0], a), base))
      .find(Boolean);
    if (lazyload) return lazyload;
    if (!fallback) {
      const srcset = attrOf(tag[0], 'srcset');
      const fromSet = srcset ? normalizeImage(String(srcset).split(/\s+/)[0], base) : null;
      fallback = fromSet || normalizeImage(attrOf(tag[0], 'src'), base);
    }
  }
  if (fallback) return fallback;

  SOURCE_TAG.lastIndex = 0;
  let src;
  while ((src = SOURCE_TAG.exec(block)) !== null) {
    const url = normalizeImage(attrOf(src[0], 'srcset')?.split(/\s+/)[0], base)
      || normalizeImage(attrOf(src[0], 'src'), base);
    if (url) return url;
  }

  VIDEO_TAG.lastIndex = 0;
  const video = VIDEO_TAG.exec(block);
  if (video) {
    const poster = normalizeImage(attrOf(video[0], 'poster'), base);
    if (poster) return poster;
  }

  const bg = BG_IMAGE.exec(block);
  return bg ? normalizeImage(bg[1], base) : null;
}

/** 从卡片块里抽标题。 */
export function titleFromBlock(block) {
  if (!block) return null;
  const anchorTags = block.match(/<a\b[^>]*>/gi) || [];
  for (const tag of anchorTags) {
    const t = cleanTitle(attrOf(tag, 'title')) || cleanTitle(attrOf(tag, 'aria-label'));
    if (t) return t;
  }
  IMG_TAG.lastIndex = 0;
  const img = IMG_TAG.exec(block);
  if (img) {
    const alt = cleanTitle(attrOf(img[0], 'alt'));
    if (alt) return alt;
  }
  const heading = firstGroup(/<h[1-4][^>]*>([\s\S]{1,200}?)<\/h[1-4]>/i, block);
  if (heading) return cleanTitle(heading);
  const titled = firstGroup(/class=["'][^"']*(?:title|name|desc)[^"']*["'][^>]*>([\s\S]{1,200}?)</i, block);
  if (titled) return cleanTitle(titled);
  const anchorText = firstGroup(/<a\b[^>]*>([\s\S]{1,200}?)<\/a>/i, block);
  return cleanTitle(anchorText);
}

/** 从卡片块里抽时长。 */
export function durationFromBlock(block) {
  if (!block) return null;
  const tags = block.match(/<(?:span|div|p|time|a)\b[^>]*>/gi) || [];
  for (const tag of tags) {
    for (const a of ['data-duration', 'data-dur', 'data-length', 'data-time', 'duration', 'content']) {
      const v = attrOf(tag, a);
      const d = normalizeDuration(v);
      if (d) return d;
    }
  }
  const labelled = DURATION_TEXT.exec(block);
  if (labelled) {
    const d = normalizeDuration(cleanText(labelled[1]));
    if (d) return d;
  }
  const loose = LOOSE_TIME.exec(block);
  return loose ? normalizeDuration(cleanText(loose[1])) : null;
}

/** 从卡片块里抽作者 / 频道。`tail` 用于块外紧邻的作者链接。 */
export function authorFromBlock(block, tail = '') {
  if (!block && !tail) return null;
  const own = block || '';
  const tags = own.match(/<(?:a|div|span|article)\b[^>]*>/gi) || [];
  for (const tag of tags) {
    const direct = cleanText(attrOf(tag, 'data-author')) || cleanText(attrOf(tag, 'data-model'))
      || cleanText(attrOf(tag, 'data-uploader'));
    if (direct) return direct;
  }
  const m = AUTHOR_LINK.exec(own) || AUTHOR_LINK.exec(tail || '');
  if (!m) return null;
  let slug = m[1];
  try {
    slug = decodeURIComponent(slug.replace(/\+/g, ' '));
  } catch {
    /* 保持原样 */
  }
  const cleaned = cleanText(slug);
  return cleaned ? cleaned.replace(/[-_]+/g, ' ') : null;
}

/**
 * 一次解析出卡片块的全部元数据。
 * @param {string} block 卡片块（cardBlockAt）
 * @param {string} base 站点根地址
 * @param {string} [tail] 块尾（cardTailAt），只用于作者回落
 * @returns {{title: string|null, cover: string|null, duration: string|null, author: string|null}}
 */
export function extractCardMeta(block, base, tail = '') {
  return {
    title: titleFromBlock(block),
    cover: coverFromBlock(block, base),
    duration: durationFromBlock(block),
    author: authorFromBlock(block, tail),
  };
}
