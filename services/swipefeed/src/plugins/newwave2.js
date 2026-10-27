/**
 * 2026-10-18 第二批（来自 tool/ 新脚本）：
 *
 *  · 18j     —— `Richy-18j.txt`（18J.TV 去广告·隐私·下载）
 *      列表 `a[href=/artdetail-<id>/]`，卡片标题在 `.post-item-title`，封面在 `background-image: url(&quot;…&quot;)`；
 *      详情页（artdetail）里直接带 `m3u8.*.com/.../index.m3u8`，不需要播放页。
 *  · avjb    —— `Richy-bv.txt` / `Richy-bv1.txt`（AVJB 去广告 + 悬浮下载）
 *      卡片 `div.item[data-vid] > a[href=https://avjb.com/video/<id>/<slug>/][title]`；
 *      详情页 `https://avjb.com/video/<id>/<slug>/` 里有 `oss.rw.*.com/.../hls/index.m3u8`。
 *  · 91rb    —— `91rb-vip-unlock.user v1.2.txt`（91rb 无限看 · VIP 直通）
 *      列表走 KVS 的 `/latest-updates/<n>/`；播放按脚本里那套「自动注册一次性账号拿 token」：
 *        POST {API}/user/register  form: username/email/pass/repass  (Device-Type: android) → content.token
 *        GET  {API}/videos/detail?id=<id>  headers: token/Authorization/access_token → content.video_url (m3u8)
 *      token 进程内缓存 50 分钟，失败自动重新注册一次。
 */
import { fetchText } from '../util/http.js';

const UA = { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36' };
const UA_REF = { ...UA, 'accept-language': 'zh-CN,zh;q=0.9' };

function decode(s) {
  return String(s || '')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ')
    .replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
}
function dur(text) {
  const m = /(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(String(text || ''));
  return m ? (m[3] ? `${m[1]}:${m[2]}:${m[3]}` : `${m[1]}:${m[2]}`) : null;
}

/* ───────────────────────────── 18J ───────────────────────────── */

const J18 = 'https://18j.tv';

function cards18j(html) {
  const out = [];
  const seen = new Set();
  const chunks = String(html).split(/<a href="\/artdetail-/).slice(1);
  for (const raw of chunks) {
    const id = (/^(\d+)\/"/.exec(raw) || [])[1];
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const seg = raw.slice(0, 1800);
    const title = decode(
      (/<div class="[^"]*post-item-title[^"]*">\s*<p>([\s\S]{0,220}?)<\/p>/.exec(seg) || [])[1]
      || (/\balt="([^"]{4,200})"/.exec(seg) || [])[1]
      || (/\btitle="([^"]{4,200})"/.exec(seg) || [])[1]
      || '',
    );
    const bg = (/background-image:\s*url\((?:&quot;|"|')?([^"'&]+)(?:&quot;|"|')?\)/.exec(seg) || [])[1] || '';
    const cover = bg && /^https?:/i.test(bg) ? bg : null;
    const date = decode((/<div class="[^"]*post-item-desc[^"]*">([\s\S]{0,160}?)<\/div>/.exec(seg) || [])[1] || '');
    out.push({
      site: '18j',
      id,
      title: title || `18J ${id}`,
      cover,
      duration: dur(date),
      pageUrl: `${J18}/artdetail-${id}/`,
      aspect: 'horizontal',
      extra: { date: date.slice(0, 30) },
    });
  }
  return out;
}

export const e18j = {
  id: '18j',
  name: '18J',
  displayName: '18J.TV',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const urls = [`${J18}/index.php/art/index/index.html?page=${page}`, `${J18}/`];
    for (const url of urls) {
      try {
        const { status, body } = await fetchText(url, { referer: J18 + '/', headers: UA_REF });
        if (status >= 400) continue;
        const items = cards18j(body);
        if (items.length) return { items, nextCursor: { page: page + 1 }, hasMore: true };
      } catch { /* 换下一个路由 */ }
    }
    return { items: [], nextCursor: null, hasMore: false };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${J18}/index.php/art/search/index.html?keyword=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    try {
      const { status, body } = await fetchText(url, { referer: J18 + '/', headers: UA_REF });
      if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
      const items = cards18j(body);
      return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
    } catch { return { items: [], nextCursor: null, hasMore: false }; }
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${J18}/artdetail-${card.id}/`;
    const { status, body } = await fetchText(pageUrl, { referer: J18 + '/', headers: UA_REF });
    if (status >= 400) throw new Error(`18j 详情页 HTTP ${status}`);
    const url = (body.match(/https?:\/\/[^"'\s<>\\]+\.m3u8[^"'\s<>\\]*/i) || [])[0];
    if (!url) throw new Error('18j 未找到 m3u8');
    return { url, type: 'hls', qualities: [{ label: '自动', url }], referer: pageUrl };
  },
};

/* ───────────────────────────── AVJB ───────────────────────────── */

const AVJB = 'https://avjb.com';

function cardsAvjb(html) {
  const out = [];
  const seen = new Set();
  const re = /<a href="(https:\/\/avjb\.com\/video\/(\d+)\/[^"]+\/)"\s+title="([^"]*)"[\s\S]{0,600}?<img[^>]+src="([^"]+)"/g;
  for (const m of String(html).matchAll(re)) {
    const id = m[2];
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({
      site: 'avjb',
      id,
      title: decode(m[3]) || `AVJB ${id}`,
      cover: m[4] && /^https?:/i.test(m[4]) ? m[4] : null,
      duration: null,
      pageUrl: m[1],
      aspect: 'horizontal',
      extra: {},
    });
  }
  return out;
}

export const avjb = {
  id: 'avjb',
  name: 'AVJB',
  displayName: 'AVJB',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const urls = page <= 1 ? [`${AVJB}/`] : [`${AVJB}/latest-updates/${page}/`, `${AVJB}/?page=${page}`];
    for (const url of urls) {
      try {
        const { status, body } = await fetchText(url, { referer: AVJB + '/', headers: UA_REF });
        if (status >= 400) continue;
        const items = cardsAvjb(body);
        if (items.length) return { items, nextCursor: { page: page + 1 }, hasMore: true };
      } catch { /* next */ }
    }
    return { items: [], nextCursor: null, hasMore: false };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${AVJB}/search/?q=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    try {
      const { status, body } = await fetchText(url, { referer: AVJB + '/', headers: UA_REF });
      if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
      const items = cardsAvjb(body);
      return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
    } catch { return { items: [], nextCursor: null, hasMore: false }; }
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${AVJB}/video/${card.id}/`;
    const { status, body } = await fetchText(pageUrl, { referer: AVJB + '/', headers: UA_REF });
    if (status >= 400) throw new Error(`avjb 详情页 HTTP ${status}`);
    // 1) 外链 m3u8（免费片走这条）
    const hls = (body.match(/https?:\/\/[^"'\s<>\\]+\.m3u8[^"'\s<>\\]*/i) || [])[0];
    if (hls) return { url: hls, type: 'hls', qualities: [{ label: '自动', url: hls }], referer: pageUrl };
    // 2) KVS 的 currentVideoURL（可能带 function/0/ 前缀）
    const raw = (body.match(/currentVideoURL\s*=\s*"([^"]+)"/) || [])[1] || '';
    const mp4 = raw.replace(/^function\/0\//, '');
    if (mp4 && /^https?:/i.test(mp4)) {
      // 探一下能不能取：免费片 206，VIP 片 404
      try {
        // 只要状态码：取 1KB 就断开
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 15000);
        const r = await fetch(mp4, { headers: { ...UA, Referer: pageUrl, Range: 'bytes=0-1023' }, signal: ctrl.signal, redirect: 'follow' });
        clearTimeout(timer);
        const ok = r.status === 200 || r.status === 206;
        await r.body?.cancel?.();
        if (ok) return { url: mp4, type: 'mp4', qualities: [{ label: '原画', url: mp4 }], referer: pageUrl };
      } catch { /* 落到下面报错 */ }
    }
    // 3) VIP 片：退到页面上的预览片段（能播，但只是预览）
    const preview = (body.match(/https?:\/\/[^"'\s<>\\]+_preview\.mp4\/?/i) || [])[0];
    if (preview) {
      return {
        url: preview,
        type: 'mp4',
        qualities: [{ label: '预览', url: preview }],
        referer: pageUrl,
        preview: true,
      };
    }
    throw new Error('avjb 该片需要 VIP（页面只有门禁地址，连预览都没有）');
  },
};

/* ───────────────────────────── 91rb ───────────────────────────── */

const RB = 'https://www.91rb.net';
const RB_API = 'https://api.vuecloudrb.com';
let rbToken = '';
let rbTokenAt = 0;

async function rbRegister() {
  const rnd = Math.random().toString(36).replace(/[^a-z0-9]/g, '').slice(0, 9);
  const user = `rb${rnd}u`;
  const pass = `Rb${rnd}91x`;
  const { status, body } = await fetchText(`${RB_API}/user/register`, {
    method: 'POST',
    referer: RB + '/',
    headers: {
      ...UA,
      'content-type': 'application/x-www-form-urlencoded',
      'device-type': 'android',
      accept: 'application/json',
    },
    body: new URLSearchParams({ username: user, email: `${user}@163.com`, pass, repass: pass }).toString(),
    timeout: 20000,
  });
  if (status >= 400) throw new Error(`91rb 注册 HTTP ${status}`);
  let json = {};
  try { json = JSON.parse(body); } catch { /* */ }
  const token = json?.content?.token;
  if (!token) throw new Error(`91rb 自动注册失败：${json?.message || 'no token'}`);
  rbToken = token;
  rbTokenAt = Date.now();
  return token;
}

async function rbTokenGet(force = false) {
  if (!force && rbToken && Date.now() - rbTokenAt < 50 * 60_000) return rbToken;
  return rbRegister();
}

function cards91rb(html) {
  const out = [];
  const seen = new Set();
  const re = /<a href="(https:\/\/www\.91rb\.net\/videos\/(\d+)\/[^"]+\/)"\s+title="([^"]*)"[\s\S]{0,700}?data-original="([^"]+)"/g;
  for (const m of String(html).matchAll(re)) {
    const id = m[2];
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({
      site: '91rb',
      id,
      title: decode(m[3]) || `91rb ${id}`,
      cover: m[4] && /^https?:/i.test(m[4]) ? m[4] : null,
      duration: null,
      pageUrl: m[1],
      aspect: 'horizontal',
      extra: {},
    });
  }
  return out;
}

export const rb91 = {
  id: '91rb',
  name: '91rb',
  displayName: '91rb',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const urls = page <= 1 ? [`${RB}/`, `${RB}/latest-updates/`] : [`${RB}/latest-updates/${page}/`];
    for (const url of urls) {
      try {
        const { status, body } = await fetchText(url, { referer: RB + '/', headers: UA_REF });
        if (status >= 400) continue;
        const items = cards91rb(body);
        if (items.length) return { items, nextCursor: { page: page + 1 }, hasMore: true };
      } catch { /* next */ }
    }
    return { items: [], nextCursor: null, hasMore: false };
  },
  async search(q, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const url = `${RB}/search/?q=${encodeURIComponent(String(q || ''))}` + (page > 1 ? `&page=${page}` : '');
    try {
      const { status, body } = await fetchText(url, { referer: RB + '/', headers: UA_REF });
      if (status >= 400) return { items: [], nextCursor: null, hasMore: false };
      const items = cards91rb(body);
      return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
    } catch { return { items: [], nextCursor: null, hasMore: false }; }
  },
  async resolve(card) {
    // 先走脚本里那套「CDN 直连」：delivery.* 不校验签名，只要 id 就能拼出清单
    const bucket = Math.floor(Number(card.id) / 1000) * 1000;
    const hosts = ['https://delivery.douyinpaly.com', 'https://delivery.douyincontent.com'];
    const suffixes = ['', '_1080p', '_720p', '_480p'];
    for (const host of hosts) {
      for (const sfx of suffixes) {
        const url = `${host}/hls/contents/videos/${bucket}/${card.id}/${card.id}${sfx}.mp4/index.m3u8`;
        try {
          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 15000);
          const r = await fetch(url, { headers: { ...UA, Referer: card.pageUrl || RB + '/' }, signal: ctrl.signal, redirect: 'follow' });
          const ok = r.status === 200;
          const text = ok ? (await r.text()).slice(0, 200) : '';
          clearTimeout(timer);
          if (ok && text.includes('#EXTM3U')) {
            return { url, type: 'hls', qualities: [{ label: '自动', url }], referer: card.pageUrl || RB + '/' };
          }
        } catch { /* 换下一个候选 */ }
      }
    }
    // 再退到站点 API（带自动注册的 token）
    const hdr = (t) => ({ ...UA, 'device-type': 'android', token: t, authorization: t, access_token: t, accept: 'application/json' });
    const ask = async (token) => {
      const { status, body } = await fetchText(`${RB_API}/videos/detail?id=${encodeURIComponent(card.id)}`, {
        referer: RB + '/', headers: hdr(token), timeout: 20000,
      });
      let json = {};
      try { json = JSON.parse(body); } catch { /* */ }
      if (status >= 400) return null;
      const c = json?.content || {};
      return c.video_url || c.video_secondary_url || c.m3u8 || c.url || null;
    };
    let url = await ask(await rbTokenGet());
    if (!url) url = await ask(await rbTokenGet(true)); // token 过期 → 重新注册一次
    if (!url) throw new Error('91rb 未取到播放地址');
    return {
      url,
      type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
      qualities: [{ label: '自动', url }],
      referer: card.pageUrl || RB + '/',
    };
  },
};
