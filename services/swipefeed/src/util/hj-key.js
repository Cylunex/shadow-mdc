/**
 * 海角（haijiao）HLS 密钥合成 —— 端口自 LumenBox `HaijiaoKeys.kt`（只读参考，实现在本仓库）。
 *
 * 站点给的清单是 AES-128 加密的，但清单里那把 `.key` 直接拿来解密会 BAD_DECRYPT。
 * 脚本（HLS 播放键 3.3.3）里的可复现规则是：
 *   1) 同目录的 `<清单名>.jpg` 其实是一段 base64 素材；
 *   2) 候选密钥有若干个后缀变体（`.key`、`.keyv1`、`.key1`、`.keyv`、`.keyv2`、`.key2`）；
 *   3) 候选密钥再和素材按字节异或：key[i] ^= jpg[i % jpg.length]；
 *   4) 用首片做校验：AES-128-CBC 无填充解密后，第 0 与第 188 字节必须是 TS 同步字节 0x47。
 * 只有通过校验的组合才当作播放密钥。
 */
import crypto from 'node:crypto';
import { fetchText, fetchRaw } from './http.js';

const TIMEOUT = 15000;
const TS_SYNC = 0x47;

/** 候选密钥地址：原样 + 若干后缀变体（与脚本一致，kv 取 1、2）。 */
export function keyCandidates(keyUri) {
  const out = new Set();
  const push = (u) => { if (u) out.add(u); };
  push(keyUri);
  for (const kv of ['1', '2']) {
    push(keyUri.replace(/\.key(\?|$)/, `.keyv${kv}$1`));
    push(keyUri.replace(/\.key(\?|$)/, `.key${kv}$1`));
    push(keyUri.replace(/\.key(\?|$)/, `.keyv$1`));
  }
  return [...out];
}

/** 清单旁边那份 base64 素材的地址。 */
export function materialUrlFor(playlistUrl) {
  return String(playlistUrl || '').replace(/\.m3u8(\?[^#]*)?$/i, '.jpg');
}

export function parseKeyLine(playlistText, baseUrl) {
  const text = String(playlistText || '').replace(/^\uFEFF/, '');
  let keyUri = null;
  let ivHex = null;
  let firstSegment = null;
  let mediaSequence = 0;
  let segmentCount = 0;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE')) {
      mediaSequence = Number(line.split(':')[1]) || 0;
    } else if (line.startsWith('#EXT-X-KEY')) {
      if (/METHOD=NONE/i.test(line)) continue;
      const uri = /URI="([^"]+)"/i.exec(line)?.[1];
      const iv = /IV=0x([0-9a-fA-F]+)/i.exec(line)?.[1];
      if (uri) keyUri = new URL(uri, baseUrl).toString();
      if (iv) ivHex = iv;
    } else if (!line.startsWith('#')) {
      segmentCount += 1;
      if (!firstSegment) firstSegment = new URL(line, baseUrl).toString();
    }
  }
  const iv = ivHex
    ? Buffer.from(ivHex.padStart(32, '0').slice(0, 32), 'hex')
    : Buffer.alloc(16, 0);
  return { keyUri, iv, firstSegment, mediaSequence, segmentCount };
}

function decryptOk(key, iv, segment) {
  if (!key || key.length !== 16 || !iv || iv.length !== 16) return false;
  if (!segment || segment.length < 376) return false;
  const usable = segment.subarray(0, segment.length - (segment.length % 16));
  try {
    const d = crypto.createDecipheriv('aes-128-cbc', key, iv);
    d.setAutoPadding(false);
    const out = Buffer.concat([d.update(usable), d.final()]);
    return out.length > 188 && out[0] === TS_SYNC && out[188] === TS_SYNC;
  } catch {
    return false;
  }
}

function mixWithMaterial(key, material) {
  if (!material || !material.length) return Buffer.from(key);
  const out = Buffer.from(key);
  for (let i = 0; i < out.length; i++) out[i] ^= material[i % material.length];
  return out;
}

async function fetchBytes(url, referer) {
  try {
    const r = await fetchRaw(url, {
      referer,
      headers: { Referer: referer || '', 'Accept-Encoding': 'identity' },
      signal: AbortSignal.timeout(TIMEOUT),
    });
    if (r.status >= 400) return null;
    return Buffer.from(await r.arrayBuffer());
  } catch {
    return null;
  }
}

/**
 * 针对“某一把 key + 某一段分片”试出真密钥。
 * 海角清单会**轮换密钥**（每个 KEY 行管到下一个 KEY 行），所以必须逐段校验，
 * 不能拿第一把密钥套整张清单。
 */
async function solveForKey(keyUri, iv, segmentUrl, material) {
  const segment = await fetchBytes(segmentUrl, 'https://www.haijiao.com/');
  if (!segment) return null;
  for (const candidate of keyCandidates(keyUri)) {
    // eslint-disable-next-line no-await-in-loop
    const raw = await fetchBytes(candidate, 'https://www.haijiao.com/');
    if (!raw || raw.length !== 16) continue;
    if (decryptOk(raw, iv, segment)) return { key: raw, keyUri: candidate };
    if (material) {
      const mixed = mixWithMaterial(raw, material);
      if (decryptOk(mixed, iv, segment)) return { key: mixed, keyUri: candidate, mixed: true };
    }
  }
  return null;
}

/** 取清单同名 `.jpg`（内容是 base64 素材）。 */
async function loadMaterial(playlistUrl) {
  const jpgUrl = materialUrlFor(playlistUrl);
  if (!jpgUrl) return null;
  const jpg = await fetchText(jpgUrl, { referer: 'https://www.haijiao.com/' });
  if (!jpg || jpg.status >= 400 || !jpg.body) return null;
  try {
    const b = Buffer.from(String(jpg.body).trim(), 'base64');
    return b.length ? b : null;
  } catch {
    return null;
  }
}

/**
 * 生成给播放器用的清单：**每把 key 单独合成**，分片地址改回本代理。
 * @param {string} playlistUrl
 * @param {string} prefix 例如 '' 或 '/swipefeed'
 */
export async function buildPlaylist(playlistUrl, prefix = '') {
  const { status, body } = await fetchText(playlistUrl, { referer: 'https://www.haijiao.com/' });
  if (status >= 400 || !body) return null;
  const material = await loadMaterial(playlistUrl);
  const lines = body.split('\n');
  const outLines = [...lines];
  const wrapSegment = (abs) =>
    `${prefix}/hls?u=${encodeURIComponent(abs)}&r=${encodeURIComponent('https://www.haijiao.com/')}`;

  let pendingIdx = -1;
  let pendingKey = null;
  let pendingIv = Buffer.alloc(16, 0);
  let solved = 0;
  let failed = 0;
  const keyCache = new Map(); // keyUri -> token（同一把 key 复用）

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#EXT-X-KEY')) {
      if (/METHOD=NONE/i.test(line)) { pendingIdx = -1; continue; }
      const uri = /URI="([^"]+)"/i.exec(line)?.[1];
      const ivHex = /IV=0x([0-9a-fA-F]+)/i.exec(line)?.[1];
      pendingKey = uri ? new URL(uri, playlistUrl).toString() : null;
      pendingIv = ivHex ? Buffer.from(ivHex.padStart(32, '0').slice(0, 32), 'hex') : Buffer.alloc(16, 0);
      pendingIdx = pendingKey ? i : -1;
      continue;
    }
    if (line.startsWith('#')) {
      outLines[i] = raw.replace(/URI="([^"]+)"/g, (m, uri) => {
        try { return `URI="${wrapSegment(new URL(uri, playlistUrl).toString())}"`; } catch { return m; }
      });
      continue;
    }
    // 分片行：如果上面挂了 KEY，就用这一段试出真密钥
    let segUrl;
    try { segUrl = new URL(line, playlistUrl).toString(); } catch { continue; }
    if (pendingIdx >= 0 && pendingKey) {
      const cacheKey = `${pendingKey}|${pendingIv.toString('hex')}`;
      let token = keyCache.get(cacheKey);
      if (!token) {
        // eslint-disable-next-line no-await-in-loop
        const found = await solveForKey(pendingKey, pendingIv, segUrl, material);
        if (found) {
          token = storeKey(found.key);
          keyCache.set(cacheKey, token);
          solved += 1;
        } else {
          failed += 1;
        }
      }
      if (token) {
        outLines[pendingIdx] = lines[pendingIdx].replace(
          /URI=(?:"[^"]*"|'[^']*'|[^,\s]+)/i,
          `URI="${prefix}/api/hj-key?t=${token}"`,
        );
      }
      pendingIdx = -1;
    }
    outLines[i] = wrapSegment(segUrl);
  }
  if (!solved) return null;
  return { playlist: outLines.join('\n'), solved, failed };
}

/** 单把 key 的合成（保留给自检脚本用）。 */
export async function synthesizeKey(playlistUrl) {
  const { status, body } = await fetchText(playlistUrl, { referer: 'https://www.haijiao.com/' });
  if (status >= 400 || !body) return null;
  const parsed = parseKeyLine(body, playlistUrl);
  if (!parsed.keyUri || !parsed.firstSegment) return null;
  const material = await loadMaterial(playlistUrl);
  const found = await solveForKey(parsed.keyUri, parsed.iv, parsed.firstSegment, material);
  if (!found) return null;
  return { key: found.key, iv: parsed.iv, playlist: body, keyUri: found.keyUri, mixedWithMaterial: !!found.mixed };
}

/** 进程内密钥暂存：播放清单里只放一次性 token，密钥本体不出内存。 */
const keyStore = new Map(); // token -> { key, expires }
const KEY_TTL_MS = 30 * 60 * 1000;

export function storeKey(key) {
  const token = crypto.randomBytes(9).toString('hex');
  keyStore.set(token, { key: Buffer.from(key), expires: Date.now() + KEY_TTL_MS });
  if (keyStore.size > 200) {
    const now = Date.now();
    for (const [t, v] of keyStore) if (v.expires < now) keyStore.delete(t);
  }
  return token;
}

export function takeKey(token) {
  const hit = keyStore.get(String(token || ''));
  if (!hit) return null;
  if (Date.now() > hit.expires) { keyStore.delete(token); return null; }
  return hit.key;
}

/** 把清单里的 KEY 行改成指向本地密钥端点，分片继续走 /api/hls。 */
export function rewriteHaijiaoPlaylist(playlistText, playlistUrl, keyToken, prefix) {
  const wrapSegment = (abs) =>
    `${prefix}/hls?u=${encodeURIComponent(abs)}&r=${encodeURIComponent('https://www.haijiao.com/')}`;
  const resolve = (raw) => {
    try { return new URL(raw, playlistUrl).toString(); } catch { return null; }
  };
  const keyUrl = `${prefix}/hj-key?t=${keyToken}`;
  const out = [];
  for (const raw of String(playlistText || '').split('\n')) {
    const line = raw.trim();
    if (!line) { out.push(raw); continue; }
    if (line.startsWith('#EXT-X-KEY')) {
      if (/METHOD=NONE/i.test(line)) { out.push(raw); continue; }
      out.push(line.replace(/URI=(?:"[^"]*"|'[^']*'|[^,\s]+)/i, `URI="${keyUrl}"`));
      continue;
    }
    if (line.startsWith('#')) {
      out.push(raw.replace(/URI="([^"]+)"/g, (m, uri) => {
        const abs = resolve(uri);
        return abs ? `URI="${wrapSegment(abs)}"` : m;
      }));
      continue;
    }
    const abs = resolve(line);
    out.push(abs ? wrapSegment(abs) : raw);
  }
  return out.join('\n');
}
