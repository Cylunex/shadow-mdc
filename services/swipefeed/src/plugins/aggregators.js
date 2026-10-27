/**
 * Aggregator SiteSource plugins (JAV/AV Chinese discovery + stream).
 * Cheap cards on list; resolve on demand. No VIP bypass / coin unlock.
 */
import {
  fetchText, fetchBuffer, unescape, firstMatch, streamFromMap, looseMp4Hls,
  absUrl, clean, collectHrefCards, unwrapPngPayload,
} from '../util/http.js';

function pageResult(items, page, extra = {}) {
  return {
    items,
    nextCursor: items.length ? { page: page + 1 } : null,
    hasMore: items.length > 0,
    ...extra,
  };
}

function msToDur(ms) {
  if (!ms || !Number.isFinite(ms)) return null;
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function secToDur(sec) {
  if (!sec || !Number.isFinite(sec)) return null;
  return msToDur(sec * 1000);
}

/* ───────────────────────── rou.video 肉視頻 ───────────────────────── */

const ROU_BASE = 'https://rou.video';

function decodeRouEv(d, k) {
  const raw = Buffer.from(d, 'base64');
  let plain = '';
  for (const b of raw) plain += String.fromCharCode(b - k);
  return JSON.parse(plain);
}

function parseRouCards(html, max = 40) {
  const seen = new Set();
  const items = [];
  const re = /\/v\/(cm[a-z0-9]+)/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    // try nearby title in SSR: name:"..." near id
    const idx = html.indexOf(`"${id}"`);
    let title = `Video ${id}`;
    let cover = null;
    let duration = null;
    if (idx >= 0) {
      const chunk = html.slice(Math.max(0, idx - 80), idx + 600);
      const nm = /name:"((?:\\.|[^"\\])*)"/.exec(chunk);
      if (nm) title = clean(nm[1].replace(/\\u([\dA-Fa-f]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))) || title;
      const cov = /coverImageUrl:"([^"]+)"/.exec(chunk);
      if (cov) cover = cov[1];
      const dur = /duration:([0-9.]+)/.exec(chunk);
      if (dur) duration = secToDur(parseFloat(dur[1]));
    }
    items.push({
      site: 'rou',
      id,
      title,
      cover,
      duration,
      author: null,
      aspect: 'unknown',
      pageUrl: `${ROU_BASE}/v/${id}`,
    });
    if (items.length >= max) break;
  }
  return items;
}

async function rouListFromWatching() {
  const { status, body } = await fetchText(`${ROU_BASE}/api/v/watching`, {
    referer: ROU_BASE,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
  });
  if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
  let arr;
  try { arr = JSON.parse(body); } catch { arr = []; }
  if (!Array.isArray(arr)) arr = [];
  const items = arr.slice(0, 40).map((v) => ({
    site: 'rou',
    id: v.id,
    title: v.nameZh || v.name || `Video ${v.id}`,
    cover: v.coverImageUrl || null,
    duration: secToDur(v.duration),
    author: null,
    aspect: 'unknown',
    pageUrl: `${ROU_BASE}/v/${v.id}`,
  }));
  return { items, _httpStatus: status, _raw: body };
}

export const rou = {
  id: 'rou',
  name: '肉視頻',
  displayName: '肉視頻',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    if (page <= 1) {
      try {
        const w = await rouListFromWatching();
        if (w.items.length) return pageResult(w.items, page, { _httpStatus: w._httpStatus, _raw: w._raw });
      } catch { /* fall through to HTML */ }
    }
    const url = page <= 1 ? `${ROU_BASE}/v` : `${ROU_BASE}/v?page=${page}`;
    const { status, body } = await fetchText(url, {
      referer: ROU_BASE,
      headers: { Accept: 'text/html' },
    });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = parseRouCards(body);
    return pageResult(items, page, { _httpStatus: status, _raw: body });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = `${ROU_BASE}/search?q=${enc}${page > 1 ? `&page=${page}` : ''}`;
    const { status, body } = await fetchText(url, {
      referer: ROU_BASE,
      headers: { Accept: 'text/html' },
    });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parseRouCards(body), page, { _httpStatus: status, _raw: body });
  },
  async categoryPage(category, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(category);
    const url = `${ROU_BASE}/t/${enc}${page > 1 ? `?page=${page}` : ''}`;
    const { status, body } = await fetchText(url, {
      referer: ROU_BASE,
      headers: { Accept: 'text/html' },
    });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parseRouCards(body), page, { _httpStatus: status, _raw: body });
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${ROU_BASE}/v/${card.id}`;
    const { body } = await fetchText(pageUrl, {
      referer: ROU_BASE,
      headers: { Accept: 'text/html' },
    });
    const ev = /\{d:"([^"]+)",k:(\d+)\}/.exec(body);
    if (!ev) throw new Error('No encrypted stream payload for ' + card.id);
    let decoded;
    try {
      decoded = decodeRouEv(ev[1], Number(ev[2]));
    } catch (e) {
      throw new Error('Failed to decode rou stream: ' + e.message);
    }
    const videoUrl = decoded.videoUrl || decoded.url;
    if (!videoUrl) throw new Error('No videoUrl in rou payload');
    const abs = absUrl(ROU_BASE, videoUrl);
    // PNG-wrapped HLS — browser prefix matches FastAPI reverse proxy (/mdc/api/swipe on NAS)
    const browserApi = (() => {
      const raw = String(process.env.SWIPEFEED_BROWSER_API_PREFIX || '').trim();
      if (raw) return (raw.startsWith('/') ? raw : `/${raw}`).replace(/\/+$/, '');
      const base = String(process.env.SWIPEFEED_BASE || '/swipefeed').trim().replace(/\/+$/, '');
      return `${base || ''}/api`.replace(/\/+/g, '/') || '/api';
    })();
    const proxyPath = `${browserApi}/png-hls?u=${encodeURIComponent(abs)}&r=${encodeURIComponent(pageUrl)}`;
    return {
      url: proxyPath,
      type: 'hls',
      qualities: [{ label: 'hls', url: proxyPath }],
      expiresHintMs: Date.now() + 30 * 60_000,
      note: 'png-wrapped-hls',
    };
  },
};

/* ───────────────────────── 134x.com 黃推長視頻 ───────────────────────── */

const X134_BASE = 'https://134x.com';

function parse134xCards(html, max = 40) {
  const items = [];
  const seen = new Set();
  // Prefer SSR objects with media mp4
  const re = /tweetId:"(\d+)",text:"((?:\\.|[^"\\])*)",media:"([^"]*)",threadsCount:\d+,durationMillis:(\d+)/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    let title = m[2]
      .replace(/\\u([\dA-Fa-f]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
      .replace(/\\n/g, ' ')
      .replace(/https?:\/\/t\.co\/\S+/g, '')
      .trim();
    title = clean(title) || `Tweet ${id}`;
    items.push({
      site: 'x134',
      id,
      title: title.slice(0, 160),
      cover: null,
      duration: msToDur(Number(m[4])),
      author: null,
      aspect: 'unknown',
      pageUrl: `${X134_BASE}/video/${id}`,
      _media: m[3] || null,
    });
    if (items.length >= max) break;
  }
  if (items.length) return items;
  // Fallback: HTML articles
  for (const art of html.matchAll(/<article class="tweet-card">([\s\S]*?)<\/article>/g)) {
    const block = art[1];
    const idm = /href="\/video\/(\d+)"/.exec(block);
    if (!idm || seen.has(idm[1])) continue;
    seen.add(idm[1]);
    const tm = /class="tweet-text[^"]*"[^>]*>([\s\S]*?)<\//.exec(block);
    let title = tm ? clean(tm[1].replace(/<[^>]+>/g, '')) : null;
    items.push({
      site: 'x134',
      id: idm[1],
      title: (title || `Tweet ${idm[1]}`).slice(0, 160),
      cover: null,
      duration: null,
      author: null,
      aspect: 'unknown',
      pageUrl: `${X134_BASE}/video/${idm[1]}`,
    });
    if (items.length >= max) break;
  }
  return items;
}

export const x134 = {
  id: 'x134',
  name: '134X長視頻',
  displayName: '134X長視頻',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${X134_BASE}/popular` : `${X134_BASE}/popular?page=${page}`;
    const { status, body } = await fetchText(url, {
      referer: X134_BASE,
      headers: { Accept: 'text/html' },
    });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parse134xCards(body), page, { _httpStatus: status, _raw: body });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    // search SSR often empty / 500 — fall back to featured filtered client-side if needed
    const url = `${X134_BASE}/search?q=${enc}${page > 1 ? `&page=${page}` : ''}`;
    const { status, body } = await fetchText(url, {
      referer: X134_BASE,
      headers: { Accept: 'text/html' },
    });
    let items = status < 400 ? parse134xCards(body) : [];
    if (!items.length) {
      const feat = await fetchText(
        page <= 1 ? `${X134_BASE}/featured` : `${X134_BASE}/featured?page=${page}`,
        { referer: X134_BASE, headers: { Accept: 'text/html' } },
      );
      const all = parse134xCards(feat.body);
      const ql = q.trim().toLowerCase();
      items = all.filter((c) => (c.title || '').toLowerCase().includes(ql));
    }
    return pageResult(items, page, { _httpStatus: status });
  },
  async categoryPage(_category, cursor) {
    // no real categories — use featured
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${X134_BASE}/featured` : `${X134_BASE}/featured?page=${page}`;
    const { status, body } = await fetchText(url, {
      referer: X134_BASE,
      headers: { Accept: 'text/html' },
    });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parse134xCards(body), page, { _httpStatus: status, _raw: body });
  },
  async resolve(card) {
    if (card._media && /\.mp4(\?|$)/i.test(card._media)) {
      return streamFromMap({ mp4: card._media });
    }
    const pageUrl = card.pageUrl || `${X134_BASE}/video/${card.id}`;
    const { body } = await fetchText(pageUrl, {
      referer: X134_BASE,
      headers: { Accept: 'text/html' },
    });
    const q = {};
    const mp4s = [...body.matchAll(/https:\/\/video\.twimg\.com[^"'\s]+\.mp4[^"'\s]*/g)].map((x) => x[0]);
    mp4s.forEach((u, i) => { q[`mp4_${i}`] = unescape(u); });
    const m3u8 = firstMatch(/https:\/\/video\.twimg\.com[^"'\s]+\.m3u8[^"'\s]*/i, body);
    if (m3u8) q.hls = unescape(m3u8);
    // SSR media field
    const media = firstMatch(/media:"(https:\/\/video\.twimg\.com[^"]+)"/, body);
    if (media) q.media = unescape(media);
    const stream = streamFromMap(q);
    if (stream) return stream;
    throw new Error('No playable stream for ' + card.id);
  },
};

/* ───────────────────────── bili-fun.com Bili影院 ───────────────────────── */

const BILI_BASE = 'https://www.bili-fun.com';

function parseBiliCards(html, max = 40) {
  const seen = new Set();
  const items = [];
  // img alt near /video/ID is the most reliable title on the home/RSC markup
  const reAlt = /href="\/video\/(\d+)"[^>]*>\s*<img[^>]+alt="([^"]+)"/gi;
  let m;
  while ((m = reAlt.exec(html)) !== null) {
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    const title = clean(m[2]) || `Video ${id}`;
    const cov = new RegExp(`href="/video/${id}"[\\s\\S]{0,300}?src="(https?://[^"]+)"`, 'i').exec(html);
    items.push({
      site: 'bilifun',
      id,
      title: title.slice(0, 160),
      cover: cov ? cov[1] : null,
      duration: null,
      author: null,
      aspect: 'unknown',
      pageUrl: `${BILI_BASE}/video/${id}`,
    });
    if (items.length >= max) break;
  }
  if (items.length >= 8) return items;
  // bare hrefs fallback
  for (const card of collectHrefCards(html, {
    site: 'bilifun',
    base: BILI_BASE,
    hrefRe: /href=["']((?:https?:\/\/(?:www\.)?bili-fun\.com)?\/video\/(\d+))["']/gi,
    max,
  })) {
    if (seen.has(card.id)) continue;
    items.push(card);
    if (items.length >= max) break;
  }
  return items;
}

export const bilifun = {
  id: 'bilifun',
  name: 'Bili影院',
  displayName: 'Bili影院',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = page <= 1 ? `${BILI_BASE}/` : `${BILI_BASE}/page/${page}`;
    const { status, body } = await fetchText(url, { referer: BILI_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parseBiliCards(body), page, { _httpStatus: status, _raw: body });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = `${BILI_BASE}/search?q=${enc}${page > 1 ? `&page=${page}` : ''}`;
    const { status, body } = await fetchText(url, { referer: BILI_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parseBiliCards(body), page, { _httpStatus: status, _raw: body });
  },
  async categoryPage(category, cursor) {
    const page = cursor?.page ?? 1;
    const map = {
      asian: 'japanese-av',
      japanese: 'japanese-av',
      amateur: 'chinese-mainland',
      milf: 'japanese-av',
      lesbian: 'others',
      gay: 'others',
    };
    const slug = map[category] || category || 'japanese-av';
    const url = page <= 1
      ? `${BILI_BASE}/category/${slug}`
      : `${BILI_BASE}/category/${slug}?page=${page}`;
    const { status, body } = await fetchText(url, { referer: BILI_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    return pageResult(parseBiliCards(body), page, { _httpStatus: status, _raw: body });
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${BILI_BASE}/video/${card.id}`;
    const { body } = await fetchText(pageUrl, { referer: BILI_BASE });
    const m3u8 = firstMatch(/https?:\/\/[^"'\\\s>]+\.m3u8[^"'\\\s>]*/i, body);
    if (m3u8) {
      const url = unescape(m3u8).replace(/\\+$/, '');
      return streamFromMap({ hls: url });
    }
    const loose = looseMp4Hls(body);
    if (loose) return loose;
    const src = firstMatch(/<source[^>]+src=["']([^"']+)/i, body);
    if (src) return streamFromMap({ src: unescape(src) });
    throw new Error('No playable stream for ' + card.id);
  },
};

/* ───────────────────────── netflav.com ───────────────────────── */

const NETFLAV_BASE = 'https://www.netflav.com';

function netflavDocsToCards(docs, max = 40) {
  const items = [];
  for (const d of docs || []) {
    const id = d.videoId || d._id;
    if (!id) continue;
    items.push({
      site: 'netflav',
      id,
      title: d.title_zh || d.title || d.title_en || d.code || `Video ${id}`,
      cover: d.preview_hp || d.preview || null,
      duration: d.duration || null,
      author: Array.isArray(d.actors) ? (d.actors.find((a) => !a.includes(':')) || null) : null,
      aspect: 'unknown',
      pageUrl: `${NETFLAV_BASE}/video?id=${id}`,
    });
    if (items.length >= max) break;
  }
  return items;
}

function extractNextState(html) {
  const m = /<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/.exec(html);
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch { return null; }
}

export const netflav = {
  id: 'netflav',
  name: 'Netflav',
  displayName: 'Netflav',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    // Home embeds censored/uncensored/chinese docs; paginated sections via /censored?page=
    let url = NETFLAV_BASE + '/';
    if (page > 1) url = `${NETFLAV_BASE}/chinese-sub?page=${page}`;
    const { status, body } = await fetchText(url, { referer: NETFLAV_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    if (!body || body.length < 100) {
      return pageResult([], page, { _httpStatus: status, _raw: body });
    }
    const data = extractNextState(body);
    const st = data?.props?.initialState || {};
    let docs =
      st.chinese?.docs ||
      st.censored?.docs ||
      st.uncensored?.docs ||
      st.trending?.docs ||
      [];
    if (!docs.length && st.cache) {
      for (const v of Object.values(st.cache)) {
        const d = v?.data?.docs || (Array.isArray(v?.data) ? null : null);
        if (v?.data?.docs?.length) { docs = v.data.docs; break; }
      }
    }
    // also href fallback
    let items = netflavDocsToCards(docs);
    if (!items.length) {
      items = collectHrefCards(body, {
        site: 'netflav',
        base: NETFLAV_BASE,
        hrefRe: /href=["']((?:https?:\/\/(?:www\.)?netflav\.com)?\/video\?id=([A-Za-z0-9_-]+))["']/gi,
      });
      // fix pageUrl query
      for (const c of items) c.pageUrl = `${NETFLAV_BASE}/video?id=${c.id}`;
    }
    return pageResult(items, page, { _httpStatus: status, _raw: body });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = `${NETFLAV_BASE}/search?keyword=${enc}${page > 1 ? `&page=${page}` : ''}`;
    const { status, body } = await fetchText(url, { referer: NETFLAV_BASE });
    if (status >= 400 || !body || body.length < 100) {
      // soft-fail: filter home docs
      const home = await this.nextPage({ page: 1 });
      const ql = q.trim().toLowerCase();
      const items = (home.items || []).filter((c) =>
        (c.title || '').toLowerCase().includes(ql) || (c.id || '').toLowerCase().includes(ql),
      );
      return pageResult(items, page, { _httpStatus: status || home._httpStatus });
    }
    const data = extractNextState(body);
    const st = data?.props?.initialState || {};
    const docs = st.search?.docs || st.all?.docs || [];
    return pageResult(netflavDocsToCards(docs), page, { _httpStatus: status, _raw: body });
  },
  async categoryPage(category, cursor) {
    const page = cursor?.page ?? 1;
    const path =
      category === 'japanese' || category === 'asian' ? '/censored' :
      category === 'amateur' ? '/uncensored' :
      '/chinese-sub';
    const url = `${NETFLAV_BASE}${path}${page > 1 ? `?page=${page}` : ''}`;
    const { status, body } = await fetchText(url, { referer: NETFLAV_BASE });
    if (status >= 400 || !body || body.length < 100) {
      return pageResult([], page, { _httpStatus: status, _raw: body });
    }
    const data = extractNextState(body);
    const st = data?.props?.initialState || {};
    const docs = st.chinese?.docs || st.censored?.docs || st.uncensored?.docs || [];
    return pageResult(netflavDocsToCards(docs), page, { _httpStatus: status, _raw: body });
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${NETFLAV_BASE}/video?id=${card.id}`;
    const { body, status } = await fetchText(pageUrl, { referer: NETFLAV_BASE });
    if (!body || body.length < 100) throw new Error(`netflav video page empty (HTTP ${status})`);
    const data = extractNextState(body);
    const v = data?.props?.initialState?.video?.data;
    const q = {};
    if (v?.src && String(v.src).startsWith('http')) q.src = v.src;
    for (const s of v?.srcs || []) {
      if (typeof s === 'string' && s.startsWith('http')) q[`src_${Object.keys(q).length}`] = s;
      else if (s?.url) q[s.label || `src_${Object.keys(q).length}`] = s.url;
      else if (s?.src) q[s.label || `src_${Object.keys(q).length}`] = s.src;
    }
    for (const s of v?.otherSrcs || []) {
      if (typeof s === 'string' && s.startsWith('http')) q[`other_${Object.keys(q).length}`] = s;
      else if (s?.url) q[s.label || `other_${Object.keys(q).length}`] = s.url;
    }
    const loose = looseMp4Hls(body);
    if (loose) return loose;
    const stream = streamFromMap(q);
    if (stream) return stream;
    // Often discovery/magnet-only — honest failure
    throw new Error('No free stream (srcs empty / magnets-only) for ' + card.id);
  },
};

/* ───────────────────────── hsex.tv 好色TV ───────────────────────── */

const HSEX_BASE = 'https://hsex.tv';

export const hsex = {
  id: 'hsex',
  name: '好色TV',
  displayName: '好色TV',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const url = `${HSEX_BASE}/list-${page}.htm`;
    const { status, body } = await fetchText(url, { referer: HSEX_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'hsex',
      base: HSEX_BASE + '/',
      hrefRe: /href=["'](video-(\d+)\.htm)["']/gi,
      titleGroup: null,
    });
    // enrich titles / cover / duration from thumbnail cards
    for (const card of items) {
      const blockRe = new RegExp(
        `<div class="thumbnail">[\\s\\S]*?href="video-${card.id}\\.htm"[\\s\\S]*?</div>\\s*</div>`,
        'i',
      );
      const block = (blockRe.exec(body) || [])[0] || '';
      const tm = /<h5><a[^>]*>([^<]+)<\/a><\/h5>/i.exec(block)
        || new RegExp(`title=\\s*"([^"]+)"[\\s\\S]{0,80}?video-${card.id}\\.htm`, 'i').exec(body)
        || new RegExp(`video-${card.id}\\.htm"[^>]*>[\\s\\S]{0,120}?title=\\s*"([^"]+)"`, 'i').exec(body);
      // background-image title attribute
      const titled = new RegExp(
        `href="video-${card.id}\\.htm"[\\s\\S]{0,120}?title=\\s*"([^"]+)"`,
        'i',
      ).exec(body);
      if (titled) card.title = clean(titled[1]) || card.title;
      else if (tm) card.title = clean(tm[1]) || card.title;
      const cov = new RegExp(
        `href="video-${card.id}\\.htm"[\\s\\S]{0,200}?url\\('([^']+)'\\)`,
        'i',
      ).exec(body);
      if (cov) card.cover = absUrl(HSEX_BASE, cov[1]);
      const dur = new RegExp(
        `href="video-${card.id}\\.htm"[\\s\\S]{0,260}?<var class="duration">\\s*([0-9:]+)`,
        'i',
      ).exec(body);
      if (dur) card.duration = clean(dur[1]);
    }
    return pageResult(items, page, { _httpStatus: status, _raw: body });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const enc = encodeURIComponent(q.trim());
    const url = `${HSEX_BASE}/search.htm?search=${enc}&sort=new${page > 1 ? `&page=${page}` : ''}`;
    const { status, body } = await fetchText(url, { referer: HSEX_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'hsex',
      base: HSEX_BASE + '/',
      hrefRe: /href=["'](video-(\d+)\.htm)["']/gi,
    });
    for (const card of items) {
      const re = new RegExp(`href="video-${card.id}\\.htm"[^>]*>\\s*([^<]{2,120})\\s*<`, 'i');
      const tm = re.exec(body);
      if (tm) card.title = clean(tm[1]) || card.title;
    }
    return pageResult(items, page, { _httpStatus: status, _raw: body });
  },
  async categoryPage(category, cursor) {
    // map to site list variants
    const page = cursor?.page ?? 1;
    const map = { amateur: 'list', asian: 'list', japanese: 'list', milf: 'long_list', lesbian: 'list', gay: 'list' };
    const prefix = map[category] || 'list';
    const url = `${HSEX_BASE}/${prefix}-${page}.htm`;
    const { status, body } = await fetchText(url, { referer: HSEX_BASE });
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
    const items = collectHrefCards(body, {
      site: 'hsex',
      base: HSEX_BASE + '/',
      hrefRe: /href=["'](video-(\d+)\.htm)["']/gi,
    });
    return pageResult(items, page, { _httpStatus: status, _raw: body });
  },
  async resolve(card) {
    const pageUrl = card.pageUrl || `${HSEX_BASE}/video-${card.id}.htm`;
    const { body } = await fetchText(pageUrl, { referer: HSEX_BASE });
    const src = firstMatch(/<source[^>]+src=["']([^"']+)/i, body)
      || firstMatch(/https?:\/\/[^"'\\\s]+\.m3u8[^"'\\\s]*/i, body)
      || firstMatch(/https?:\/\/[^"'\\\s]+\.mp4[^"'\\\s]*/i, body);
    if (src) {
      const url = unescape(src);
      return streamFromMap({ [/\.m3u8/i.test(url) ? 'hls' : 'mp4']: url });
    }
    const loose = looseMp4Hls(body);
    if (loose) return loose;
    throw new Error('No playable stream for ' + card.id);
  },
};

/* ───────────────────────── av.telstra.com.cv 聚合影视 ───────────────────────── */

const AGGR_BASE = 'https://av.telstra.com.cv';
const AGGR_SPIDERS = ['avgood', 'b8xx6', 'avxq']; // adult sources with home lists

async function aggrJson(path) {
  const { status, body } = await fetchText(`${AGGR_BASE}${path}`, {
    referer: AGGR_BASE,
    headers: { Accept: 'application/json' },
  });
  if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
  try {
    return { status, data: JSON.parse(body), raw: body };
  } catch (e) {
    throw Object.assign(new Error('Invalid JSON from aggrcine'), { status, body });
  }
}

function aggrVodToCard(vod, spider) {
  const id = `${spider}::${vod.id}`;
  return {
    site: 'aggrcine',
    id,
    title: vod.name || `Video ${vod.id}`,
    cover: vod.pic || null,
    duration: vod.remarks && /^\d/.test(vod.remarks) ? vod.remarks : null,
    author: spider,
    aspect: 'unknown',
    pageUrl: `${AGGR_BASE}/s/${spider}/d/${encodeURIComponent(vod.id)}`,
    _spider: spider,
    _vodId: String(vod.id),
  };
}

export const aggrcine = {
  id: 'aggrcine',
  name: '聚合影视',
  displayName: '聚合影视',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const spiderIdx = (page - 1) % AGGR_SPIDERS.length;
    const spider = AGGR_SPIDERS[spiderIdx];
    // Prefer home list; for deeper pages use first category
    let items = [];
    let raw = '';
    let status = 200;
    if (page <= AGGR_SPIDERS.length) {
      const res = await aggrJson(`/api/v1/spiders/${spider}/home`);
      status = res.status;
      raw = res.raw;
      const list = res.data?.list || [];
      items = list.map((v) => aggrVodToCard(v, spider));
      if (!items.length && res.data?.categories?.[0]?.tid) {
        const tid = res.data.categories[0].tid;
        const cat = await aggrJson(
          `/api/v1/spiders/${spider}/category?tid=${encodeURIComponent(tid)}&pg=1`,
        );
        items = (cat.data?.list || []).map((v) => aggrVodToCard(v, spider));
        raw = cat.raw;
        status = cat.status;
      }
    } else {
      const deepPage = Math.floor((page - 1) / AGGR_SPIDERS.length) + 1;
      const home = await aggrJson(`/api/v1/spiders/${spider}/home`);
      const tid = home.data?.categories?.[0]?.tid || '664';
      const cat = await aggrJson(
        `/api/v1/spiders/${spider}/category?tid=${encodeURIComponent(tid)}&pg=${deepPage}`,
      );
      status = cat.status;
      raw = cat.raw;
      items = (cat.data?.list || []).map((v) => aggrVodToCard(v, spider));
    }
    return pageResult(items, page, { _httpStatus: status, _raw: raw });
  },
  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const spider = AGGR_SPIDERS[(page - 1) % AGGR_SPIDERS.length];
    const pg = Math.floor((page - 1) / AGGR_SPIDERS.length) + 1;
    const res = await aggrJson(
      `/api/v1/spiders/${spider}/search?wd=${encodeURIComponent(q.trim())}&pg=${pg}`,
    );
    const items = (res.data?.list || []).map((v) => aggrVodToCard(v, spider));
    return pageResult(items, page, { _httpStatus: res.status, _raw: res.raw });
  },
  async categoryPage(category, cursor) {
    return this.nextPage(cursor);
  },
  async resolve(card) {
    let spider = card._spider;
    let vodId = card._vodId;
    if (!spider || !vodId) {
      const parts = String(card.id).split('::');
      if (parts.length >= 2) {
        spider = parts[0];
        vodId = parts.slice(1).join('::');
      }
    }
    if (!spider || !vodId) throw new Error('Missing spider/vod id');
    const detail = await aggrJson(
      `/api/v1/spiders/${spider}/detail?id=${encodeURIComponent(vodId)}`,
    );
    const eps = detail.data?.episodes || [];
    if (!eps.length) throw new Error('No episodes for ' + card.id);
    const ep = eps[0];
    const play = await aggrJson(
      `/api/v1/spiders/${spider}/play?flag=${encodeURIComponent(ep.flag || spider)}&id=${encodeURIComponent(ep.id)}`,
    );
    const url = play.data?.url;
    if (!url || !String(url).startsWith('http')) throw new Error('No play url for ' + card.id);
    return {
      url,
      type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
      qualities: [{ label: ep.name || 'default', url }],
      expiresHintMs: Date.now() + 45 * 60_000,
      headers: play.data?.header || undefined,
    };
  },
};

export const AGGREGATOR_SOURCES = [rou, x134, bilifun, netflav, hsex, aggrcine];
