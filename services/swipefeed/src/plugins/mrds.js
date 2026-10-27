/**
 * 每日大赛 / Mirages（mrds.com）—— 端口自 tool 目录新脚本
 * `frmlhxlar-hls-downloader2.user.txt`（镜像站 HLS 下载 + 去广告 v1.6.5）。
 *
 * 站点是 Typecho + Mirages 主题：列表和详情都是**静态 HTML**，不用跑浏览器：
 *   · 列表：`/` 第 1 页，`/page/<N>/` 之后每页 60 条（归档链接 `/archives/<id>/`）
 *   · 卡片：`<h2 class="post-card-title">` 标题 + `loadBannerDirect('<封面>')`
 *   · 播放：详情页里藏着 `"video":{"url":"https:\/\/…m3u8?auth_key=…","type":"hls"}`
 * 脚本里那套「归档页 m3u8 解密」就是把这个 JSON 里的转义还原出来，这里直接解析。
 */
import { fetchText } from '../util/http.js';

const BASE = 'https://mrds.com';
const REF = BASE + '/';

const CARD_RE = /<a href="\/archives\/(\d+)\/"[^>]*>([\s\S]{0,2400}?)<\/a>/g;
const COVER_RE = /loadBannerDirect\(\s*'([^']+)'/;
const TITLE_RE = /<h2[^>]*post-card-title[^>]*>([\s\S]{1,300}?)<\/h2>/i;
const FALLBACK_TITLE_RE = /itemprop="headline"[^>]*>([\s\S]{1,300}?)</i;
const TAGS_RE = /<div class="post-card-info">([\s\S]{0,600}?)<\/div>/i;

function clean(html) {
  return String(html || '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\s+/g, ' ')
    .trim();
}

function parseCards(html) {
  const out = [];
  const seen = new Set();
  CARD_RE.lastIndex = 0;
  let m;
  while ((m = CARD_RE.exec(html)) !== null) {
    const id = m[1];
    const block = m[2] || '';
    if (seen.has(id)) continue;
    seen.add(id);
    const title = clean(TITLE_RE.exec(block)?.[1] || FALLBACK_TITLE_RE.exec(block)?.[1] || '');
    if (!title) continue;
    const cover = COVER_RE.exec(block)?.[1] || null;
    const info = clean(TAGS_RE.exec(block)?.[1] || '');
    const author = /^([^•]+)•/.exec(info)?.[1]?.trim() || null;
    out.push({
      site: 'mrds',
      id,
      title,
      cover: cover ? cover.replace(/^https?:\/\//i, 'https://').replace(/\/{2,}/g, '/').replace('https:/', 'https://') : null,
      duration: null,
      author,
      pageUrl: `${BASE}/archives/${id}/`,
      aspect: 'horizontal',
      extra: { tags: info.slice(-60) },
    });
  }
  return out;
}

/** 详情页 → 播放地址（`"video":{...}` 里的转义 JSON）。 */
export function pickStream(html) {
  const text = String(html || '');
  const urls = [];
  const push = (u) => {
    if (!u) return;
    const clean2 = u.replace(/\\\//g, '/').replace(/\\u0026/g, '&').trim();
    if (/^https?:\/\//i.test(clean2) && !urls.includes(clean2)) urls.push(clean2);
  };
  const vids = [...text.matchAll(/"video(?:_h265|_h264)?"\s*:\s*\{[^}]{0,400}?\}/g)].map((x) => x[0]);
  for (const raw of vids) {
    for (const m of raw.matchAll(/"url"\s*:\s*"([^"]+)"/g)) push(m[1]);
  }
  if (!urls.length) {
    for (const m of text.matchAll(/https?:\\?\/\\?\/[^"'\s<>]+\.m3u8[^"'\s<>]*/gi)) push(m[0]);
  }
  const hls = urls.find((u) => /\.m3u8/i.test(u));
  const mp4 = urls.find((u) => /\.mp4/i.test(u));
  return { hls: hls || null, mp4: mp4 || null, all: urls };
}

export const mrds = {
  id: 'mrds',
  name: 'Mrds',
  displayName: '每日大赛（镜像站）',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = page <= 1 ? `${BASE}/` : `${BASE}/page/${page}/`;
    const { status, body } = await fetchText(url, { referer: REF });
    if (status >= 400) throw Object.assign(new Error(`mrds HTTP ${status}`), { status });
    const items = parseCards(body);
    return {
      items,
      nextCursor: items.length ? { page: page + 1 } : null,
      hasMore: items.length > 0,
    };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const enc = encodeURIComponent(String(q || '').trim());
    const url = `${BASE}/search/${enc}/` + (page > 1 ? `page/${page}/` : '');
    const { status, body } = await fetchText(url, { referer: REF });
    if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
    const items = parseCards(body);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    const pageUrl = card.pageUrl && /^https?:/i.test(card.pageUrl)
      ? card.pageUrl
      : `${BASE}/archives/${card.id}/`;
    const { status, body } = await fetchText(pageUrl, { referer: REF });
    if (status >= 400) throw new Error(`mrds 详情页 HTTP ${status}`);
    const picked = pickStream(body);
    const url = picked.hls || picked.mp4;
    if (!url) throw new Error('mrds 未找到播放地址（页面结构可能变了）');
    return {
      url,
      type: picked.hls ? 'hls' : 'mp4',
      qualities: [{ label: '默认', url }],
      referer: pageUrl,
      expiresHintMs: Date.now() + 30 * 60_000,
    };
  },
};
