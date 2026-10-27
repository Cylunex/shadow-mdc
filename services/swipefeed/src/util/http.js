/** Shared HTTP fetch with browser-like UA (server-side, avoids CORS).
 * Optional HTTP(S)_PROXY / PROXY_URL via undici EnvHttpProxyAgent / ProxyAgent.
 * Legitimate public-fetch hardening only: proxy + headers + one retry.
 * No captcha solvers, cookie forging, TLS fingerprint spoof, or WAF bypass.
 */

import { ProxyAgent, EnvHttpProxyAgent, fetch as undiciFetch } from 'undici';
import { sessionHeaders } from './session-store.js';
import { cardBlockRange, cardTailAt, extractCardMeta } from './card-parse.js';

/**
 * 按域名注入本地保管的站点会话（Cookie / Referer / 额外头）。
 * 只有用户显式导入过该域名的会话才会附带；未导入时行为与之前完全一致。
 */
function withSession(url, headers) {
  const s = sessionHeaders(url);
  if (!s) return headers;
  return { ...headers, ...s };
}

export const MOBILE_UA =
  'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36';

export const DESKTOP_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

const DEFAULT_TIMEOUT = 12000;

let dispatcher = null;
let proxyStatus = 'off';

function initProxy() {
  if (dispatcher) return dispatcher;
  const explicit =
    process.env.PROXY_URL ||
    process.env.HTTPS_PROXY ||
    process.env.HTTP_PROXY ||
    process.env.https_proxy ||
    process.env.http_proxy ||
    '';
  try {
    if (explicit) {
      dispatcher = new ProxyAgent(explicit);
      proxyStatus = `ProxyAgent:${explicit.replace(/\/\/.*@/, '//***@')}`;
    } else {
      // Honors HTTP(S)_PROXY / NO_PROXY from env when set later
      dispatcher = new EnvHttpProxyAgent();
      const envProxy =
        process.env.HTTPS_PROXY ||
        process.env.HTTP_PROXY ||
        process.env.https_proxy ||
        process.env.http_proxy;
      proxyStatus = envProxy ? `EnvHttpProxyAgent:${envProxy}` : 'EnvHttpProxyAgent(none)';
    }
  } catch (e) {
    dispatcher = null;
    proxyStatus = `error:${e.message}`;
  }
  return dispatcher;
}

export function getProxyStatus() {
  initProxy();
  return proxyStatus;
}

/**
 * Raw fetch through the same dispatcher/proxy as fetchText — 供需要自己解析响应的模块复用
 * （如海角的 JSON 接口），避免绕过 PROXY_URL / HTTP(S)_PROXY。
 * @param {string} url
 * @param {object} fetchOpts 传给 fetch 的选项（signal / headers / method / body …）
 */
export async function fetchRaw(url, fetchOpts = {}) {
  const opts = { ...fetchOpts, headers: withSession(url, fetchOpts.headers || {}) };
  return doFetch(url, opts);
}

/**
 * Origin Referer for a URL when caller did not pass one.
 */
function originReferer(url) {
  try {
    const u = new URL(url);
    return u.origin + '/';
  } catch {
    return undefined;
  }
}

/**
 * @param {string} url
 * @param {object} fetchOpts
 * @param {boolean} preferProxy
 */
async function doFetch(url, fetchOpts, preferProxy = true) {
  const agent = initProxy();
  if (preferProxy && agent && proxyStatus.startsWith('ProxyAgent')) {
    try {
      return await undiciFetch(url, { ...fetchOpts, dispatcher: agent });
    } catch (e) {
      // Proxy TLS/CONNECT failed — fall back to direct (may still fail on locked-down NAS)
      proxyStatus = proxyStatus.includes('fallback')
        ? proxyStatus
        : proxyStatus + '+fallback-direct';
    }
  }
  if (agent && !proxyStatus.startsWith('ProxyAgent')) {
    try {
      return await undiciFetch(url, { ...fetchOpts, dispatcher: agent });
    } catch {
      /* continue to global fetch */
    }
  }
  return fetch(url, fetchOpts);
}

function looksLikeSoftBlock(status, body) {
  if (status === 403 || status === 503 || status === 429) return true;
  const low = (body || '').slice(0, 4000).toLowerCase();
  if (/just a moment|attention required|cf-ray|challenge-platform/i.test(low)) return true;
  if (!body || body.length < 200) return true;
  return false;
}

/**
 * @param {string} url
 * @param {{ referer?: string, timeout?: number, headers?: Record<string,string>, method?: string, body?: string, retry?: boolean, desktop?: boolean }} [opts]
 * @returns {Promise<{ status: number, body: string, finalUrl: string, headers: Headers }>}
 */
export async function fetchText(url, opts = {}) {
  const referer = opts.referer || originReferer(url);
  const ua = opts.desktop ? DESKTOP_UA : MOBILE_UA;
  const baseHeaders = {
    'User-Agent': ua,
    Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Cache-Control': 'no-cache',
    Pragma: 'no-cache',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': referer ? 'same-origin' : 'none',
    'Sec-Fetch-User': '?1',
    ...(referer ? { Referer: referer } : {}),
    ...(opts.headers || {}),
  };

  const attempt = async (headers) => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), opts.timeout ?? DEFAULT_TIMEOUT);
    try {
      const fetchOpts = {
        signal: ctrl.signal,
        redirect: 'follow',
        method: opts.method || 'GET',
        headers: withSession(url, headers),
        ...(opts.body != null ? { body: opts.body } : {}),
      };
      const res = await doFetch(url, fetchOpts);
      const body = await res.text();
      return { status: res.status, body, finalUrl: res.url, headers: res.headers };
    } finally {
      clearTimeout(t);
    }
  };

  const allowRetry = opts.retry !== false;
  let result;
  try {
    result = await attempt(baseHeaders);
  } catch (e) {
    if (!allowRetry) throw e;
    await new Promise((r) => setTimeout(r, 350));
    // One retry with desktop UA (still public headers only)
    const retryHeaders = {
      ...baseHeaders,
      'User-Agent': DESKTOP_UA,
      'Sec-Fetch-Site': 'none',
    };
    return attempt(retryHeaders);
  }

  if (allowRetry && looksLikeSoftBlock(result.status, result.body)) {
    await new Promise((r) => setTimeout(r, 350));
    const retryHeaders = {
      ...baseHeaders,
      'User-Agent': DESKTOP_UA,
      'Sec-Fetch-Site': 'none',
    };
    try {
      const second = await attempt(retryHeaders);
      // Prefer second if it looks healthier
      if (!looksLikeSoftBlock(second.status, second.body) || second.body.length > result.body.length) {
        return second;
      }
    } catch {
      /* keep first */
    }
  }
  return result;
}

/**
 * Binary fetch (for PNG-wrapped HLS etc.)
 * @returns {Promise<{ status: number, buffer: ArrayBuffer, finalUrl: string, contentType: string }>}
 */
export async function fetchBuffer(url, opts = {}) {
  const referer = opts.referer || originReferer(url);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), opts.timeout ?? DEFAULT_TIMEOUT);
  const headers = {
    'User-Agent': MOBILE_UA,
    Accept: '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    ...(referer ? { Referer: referer } : {}),
    ...(opts.headers || {}),
  };
  const run = async (hdrs) => {
    const fetchOpts = { signal: ctrl.signal, redirect: opts.redirect || 'follow', headers: withSession(url, hdrs) };
    const res = await doFetch(url, fetchOpts);
    const buffer = await res.arrayBuffer();
    return {
      status: res.status,
      buffer,
      finalUrl: res.url,
      contentType: res.headers.get('content-type') || '',
    };
  };
  try {
    try {
      return await run(headers);
    } catch (e) {
      // one retry
      await new Promise((r) => setTimeout(r, 300));
      return await run({ ...headers, 'User-Agent': DESKTOP_UA });
    }
  } finally {
    clearTimeout(t);
  }
}

export function unescape(s) {
  return String(s || '')
    .replace(/\\\//g, '/')
    .replace(/\\u0026/g, '&')
    .replace(/&amp;/g, '&')
    .replace(/\\"/g, '"')
    .trim();
}

export function absUrl(base, raw) {
  if (!raw) return raw;
  if (raw.startsWith('http')) return raw;
  if (raw.startsWith('//')) return 'https:' + raw;
  if (raw.startsWith('/')) return base.replace(/\/$/, '') + raw;
  return base.replace(/\/$/, '') + '/' + raw;
}

export function clean(s) {
  if (!s) return null;
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t || null;
}

export function firstMatch(re, text, group = 1) {
  const m = re.exec(text);
  return m?.[group]?.trim() || null;
}

/**
 * 命中 href → 卡片。卡片上的 title / cover / duration / author 由
 * `card-parse.js` 在该 href 所在的卡片块内局部解析（`rich: false` 可关闭）。
 */
export function collectHrefCards(html, {
  site,
  base,
  hrefRe,
  idGroup = 2,
  pathGroup = 1,
  titleGroup = null,
  max = 40,
  rich = true,
}) {
  const seen = new Set();
  const out = [];
  const re = new RegExp(hrefRe.source, hrefRe.flags.includes('g') ? hrefRe.flags : hrefRe.flags + 'g');
  let m;
  while ((m = re.exec(html)) !== null) {
    const id = m[idGroup];
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const path = m[pathGroup] || '';
    const explicitTitle = titleGroup != null ? clean(m[titleGroup]) : null;
    let meta = null;
    if (rich) {
      const range = cardBlockRange(html, m.index, m.index + m[0].length);
      const block = html.slice(range.from, range.to);
      meta = extractCardMeta(block, base, cardTailAt(html, range.to));
    }
    out.push({
      site,
      id,
      title: explicitTitle || (meta && meta.title) || `Video ${id}`,
      cover: (meta && meta.cover) || null,
      duration: (meta && meta.duration) || null,
      author: (meta && meta.author) || null,
      aspect: 'unknown',
      pageUrl: absUrl(base, path).split('?')[0],
    });
    if (out.length >= max) break;
  }
  return out;
}

export function streamFromMap(qualities) {
  // 有些站（Bili影院）把清单写成 "…index.m3u8\"，留着反斜杠拉流会 404
  const entries = Object.entries(qualities)
    .map(([k, u]) => [k, typeof u === 'string' ? u.trim().replace(/[\\\s]+$/, '') : u])
    .filter(([, u]) => u && u.startsWith('http'));
  if (!entries.length) return null;
  entries.sort((a, b) => {
    const da = parseInt(String(a[0]).replace(/\D/g, ''), 10) || 0;
    const db = parseInt(String(b[0]).replace(/\D/g, ''), 10) || 0;
    return db - da;
  });
  // Prefer progressive MP4 when available — native <video> + m3u8 stalls on Chrome/Android without hls.js
  const prefer =
    entries.find(([, u]) => /\.mp4(\?|$)/i.test(u)) ||
    entries.find(([k]) => /mp4/i.test(String(k)) && !/m3u8|hls/i.test(String(k))) ||
    entries.find(([k, u]) => /m3u8/i.test(u) || /hls/i.test(String(k))) ||
    entries.find(([k]) => /high|1080|720/i.test(String(k))) ||
    entries[0];
  const url = prefer[1];
  return {
    url,
    type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
    qualities: entries.map(([label, u]) => ({ label, url: u })),
    expiresHintMs: Date.now() + 45 * 60_000,
  };
}

export function looseMp4Hls(html, max = 6) {
  const found = [];
  const re = /["'](https?:\/\/[^"']+\.(?:mp4|m3u8)[^"']*)["']/gi;
  let m;
  while ((m = re.exec(html)) !== null && found.length < max * 4) {
    found.push(unescape(m[1]));
  }
  // 页面里常混着 preview/截图版的小视频：真正的正片一般在 /get_file/ 或带分辨率段
  const scored = found
    .map((u) => {
      let score = 0;
      if (/\/get_file\//i.test(u)) score += 6;
      if (/\.m3u8/i.test(u)) score += 2;
      if (/\/(?:1080|720|480)p?_?\d*\.mp4/i.test(u) || /_\d{3,4}p\.mp4/i.test(u)) score += 4;
      if (/preview|screenshot|trailer|sample|thumb/i.test(u)) score -= 8;
      return { u, score };
    })
    .filter((x) => x.score > -8)
    .sort((a, b) => b.score - a.score)
    .slice(0, max);
  const q = {};
  // 注意方向：streamFromMap 按 key 里的数字**降序**取首个可播地址，
  // 所以最好的候选要给最大编号（scored 已按优到劣排好，这里反着编号）。
  const last = scored.length - 1;
  scored.forEach((x, i) => { q[`q${last - i}`] = x.u; });
  return streamFromMap(q);
}

export function detectBlockNotes(status, body) {
  const notes = [];
  if (status === 403) notes.push('http-403');
  if (status === 503) notes.push('http-503');
  if (status === 429) notes.push('rate-limit');
  const low = (body || '').slice(0, 8000).toLowerCase();
  if (/cloudflare|cf-ray|just a moment|attention required/i.test(low)) notes.push('cloudflare');
  if (/age.?gate|are you 18|confirm.?age|adult.?content/i.test(low)) notes.push('age-gate');
  if (/access.?denied|blocked|captcha/i.test(low)) notes.push('blocked');
  if (!body || body.length < 200) notes.push('empty-or-tiny');
  return notes;
}

/** Unwrap rou.video-style PNG container with roUd chunk (optional deflate). */
export async function unwrapPngPayload(arrayBuffer) {
  const PNG_SIG = [137, 80, 78, 71, 13, 10, 26, 10];
  const ROUD = 0x726f5564; // 'roUd'
  const u8 = new Uint8Array(arrayBuffer);
  const view = new DataView(arrayBuffer);
  for (let i = 0; i < PNG_SIG.length; i++) {
    if (u8[i] !== PNG_SIG[i]) return null;
  }
  let offset = PNG_SIG.length;
  const { inflateRawSync, inflateSync } = await import('zlib');
  while (offset + 8 <= u8.length) {
    const length = view.getUint32(offset);
    const type = view.getUint32(offset + 4);
    const dataStart = offset + 8;
    if (dataStart + length > u8.length) break;
    if (type === ROUD) {
      const flags = u8[dataStart];
      let payload = Buffer.from(u8.subarray(dataStart + 1, dataStart + length));
      if (flags & 1) {
        try {
          payload = inflateSync(payload);
        } catch {
          payload = inflateRawSync(payload);
        }
      }
      return payload;
    }
    offset = dataStart + length + 4; // + CRC
  }
  return null;
}
