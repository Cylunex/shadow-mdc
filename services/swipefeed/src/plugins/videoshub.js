/**
 * videoshub — 整合 GitHub 项目 moluhualuo/-VIP-（VideosHub 视频下载器）的 Python 实现。
 *
 * 上游只用标准库 + pycryptodome：自定义 protobuf 信封、X-Sign 请求签名、
 * /p2/preview/ → /api/s3/p2/full/... 的 HMAC 媒体签名，以及 AES-128-CBC 分段解密。
 * 这里把这些逻辑逐条搬到 Node：
 *
 *   · protobuf（手写编解码，不用依赖）—— 信封 {1:code, 2:msg, 3:data}
 *   · apiSign   = sha256(secret \n METHOD \n /path(去掉/api前缀) \n query \n ts \n dev)
 *   · mediaSign = hmac_sha256(secret, "full|vid|line|quality|index.m3u8|ts|nonce")[:16]
 *   · 列表 GET  /api/v1/video/list?page=&page_size=   → 行 {1:id, 2:title, 3:cover, 5:duration(s), 8..}
 *   · 预览 GET  /api/videos/{id}/preview              → data.5.1.2 = preview URL
 *   · 清单 GET  /api/s3/p2/full/{vid}/{line}/{quality}/index.m3u8?line=&t=&n=&s=
 *   · 分片 AES-128-CBC，key 由清单里的 #EXT-X-KEY URI 提供（同一批 URL 自带签名）
 *
 * 播放：把签名后的清单地址交给浏览器 hls.js（CDN 返回 ACAO: *，key/分片都能取）。
 * 下载：downloadAndMerge() 按 videoshub 的做法并发取分片、内存顺序落盘、拼成 .ts，
 *       有 ffmpeg 就 -c copy 转 MP4（搬压缩包不重编码），没有就返回 .ts。
 */
import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';
import { fetchRaw } from '../util/http.js';

const BASE = 'https://d26fh0cuir1thg.r78k4dw47n.asia';
const SECRET = '6b5hR#7uVhJPZT1zAgA26fPSyx9Zh!Za'; // 上游仓库里的同一把密钥
const UA = 'Mozilla/5.0';
const DEFAULT_QUALITY = '720p'; // 480p 服务端直接 403（上游 README 里已记录）
const QUALITIES = ['720p', '1080p'];
const PAGE_SIZE = 30;
const CALL_TIMEOUT = 15000;

/* ─────────────── protobuf（手写，零依赖） ─────────────── */

function vint(n) {
  const out = [];
  let v = Number(n);
  for (;;) {
    const b = v & 0x7f;
    v = Math.floor(v / 128);
    out.push(v ? b | 0x80 : b);
    if (!v) break;
  }
  return Buffer.from(out);
}

function readVarint(buf, i) {
  let r = 0;
  let s = 0;
  for (;;) {
    const x = buf[i];
    i += 1;
    r |= (x & 0x7f) * 2 ** s;
    s += 7;
    if (!(x & 0x80)) return [r, i];
  }
}

function fields(input) {
  const buf = Buffer.isBuffer(input) ? input : Buffer.from(input);
  const out = [];
  let i = 0;
  while (i < buf.length) {
    let t;
    [t, i] = readVarint(buf, i);
    const fn = t >> 3;
    const wt = t & 7;
    if (wt === 0) {
      let v;
      [v, i] = readVarint(buf, i);
      out.push([fn, 0, v]);
    } else if (wt === 2) {
      let L;
      [L, i] = readVarint(buf, i);
      out.push([fn, 2, buf.subarray(i, i + L)]);
      i += L;
    } else if (wt === 1) {
      out.push([fn, 1, buf.subarray(i, i + 8)]);
      i += 8;
    } else if (wt === 5) {
      out.push([fn, 5, buf.subarray(i, i + 4)]);
      i += 4;
    } else {
      break;
    }
  }
  return out;
}

const fbytes = (ff, n) => (ff.find((f) => f[0] === n && f[1] === 2) || [0, 2, Buffer.alloc(0)])[2];
const fstr = (ff, n) => fbytes(ff, n).toString('utf8');
const fint = (ff, n) => (ff.find((f) => f[0] === n && f[1] === 0) || [0, 0, 0])[2];

function fieldStr(field, value) {
  const b = Buffer.from(String(value), 'utf8');
  return Buffer.concat([vint((field << 3) | 2), vint(b.length), b]);
}

/* ─────────────── 请求签名 ─────────────── */

const hex = (n) => crypto.randomBytes(n).toString('hex');

function apiSign(method, pathname, query, ts, dev) {
  const msg = [SECRET, method.toUpperCase(), pathname.replace(/^\/api/, ''), query, String(ts), dev].join('\n');
  return crypto.createHash('sha256').update(msg).digest('hex');
}

function mediaSign(videoId, line, quality, relPath, ts, nonce) {
  const msg = ['full', String(videoId), line, quality, relPath, String(ts), nonce].join('|');
  return crypto.createHmac('sha256', SECRET).update(msg).digest('hex').slice(0, 16);
}

/* ─────────────── 客户端（设备登录 + 会话） ─────────────── */

class VideosHubClient {
  constructor() {
    this.dev = hex(16);
    this.cookie = '';
    this.session = null;
  }

  async call(method, pathname, { body = null, auth = true, timeout = CALL_TIMEOUT } = {}) {
    const url = new URL(BASE + pathname);
    const ts = Math.floor(Date.now() / 1000);
    const nonce = hex(8);
    const headers = {
      Accept: 'application/x-protobuf',
      'X-Device-Id': this.dev,
      'X-Timestamp': String(ts),
      'X-Nonce': nonce,
      'X-Sign': apiSign(method, url.pathname, url.search.slice(1), ts, this.dev),
      'X-Client-Platform': 'h5',
      'User-Agent': UA,
    };
    if (auth && this.session) headers.Authorization = 'Bearer ' + this.session;
    if (this.cookie) headers.Cookie = this.cookie;
    if (body) headers['Content-Type'] = 'application/x-protobuf';

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let res;
    try {
      res = await fetchRaw(url.toString(), {
        method, headers, body: body || undefined, signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    for (const h of (typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [])) {
      const nv = h.split(';')[0];
      if (/^(ma_sid|store)=/i.test(nv)) this.cookie = (this.cookie ? this.cookie + '; ' : '') + nv;
    }
    let raw = Buffer.from(await res.arrayBuffer());
    if ((res.headers.get('x-protobuf-response-encoding') || '').toLowerCase() === 'base64') {
      raw = Buffer.from(raw.toString('utf8').trim(), 'base64');
    }
    const env = fields(raw);
    const code = fint(env, 1);
    const msg = fstr(env, 2);
    if (code !== 0) throw new Error(`videoshub ${pathname} code=${code} ${msg}`.trim());
    return fbytes(env, 3);
  }

  async login() {
    if (this.session) return this.session;
    const body = Buffer.concat([
      fieldStr(1, this.dev),
      fieldStr(2, 'web'),
      fieldStr(3, 'h5b'),
      fieldStr(4, 'h5:1'),
    ]);
    const data = await this.call('POST', '/api/v1/auth/device', { body, auth: false });
    this.session = fstr(fields(data), 7);
    if (!this.session) throw new Error('videoshub 设备登录未拿到会话');
    return this.session;
  }
}

/** 进程级复用：设备会话 + 列表缓存，避免每次请求都重新登录。 */
let sharedClient = null;
async function client() {
  if (!sharedClient) sharedClient = new VideosHubClient();
  await sharedClient.login();
  return sharedClient;
}

function resetClient() {
  sharedClient = null;
}

/* ─────────────── 列表 ─────────────── */

function secToDur(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n <= 0) return null;
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.floor(n % 60);
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function toCard(row) {
  const id = fstr(row, 1);
  if (!id) return null;
  const cover = fstr(row, 3);
  return {
    site: 'videoshub',
    id,
    title: (fstr(row, 2) || `Video ${id}`).slice(0, 160),
    cover: cover ? new URL(cover, BASE).toString() : null,
    duration: secToDur(fint(row, 5)),
    author: null,
    views: fint(row, 9) ? String(fint(row, 9)) : null,
    aspect: 'landscape',
    pageUrl: `https://youtudou.vip/video/${id}`,
  };
}

function decodePage(data) {
  const top = fields(data);
  const rows = top.filter((f) => f[0] === 1 && f[1] === 2).map((f) => fields(f[2]));
  const items = rows.map(toCard).filter(Boolean);
  // f2 是分页信息（{1:page, 2:page_size, 3:total, 4:has_more} 一类）
  const pageMsg = top.find((f) => f[0] === 2 && f[1] === 2);
  const pf = pageMsg ? fields(pageMsg[2]) : [];
  const cur = fint(pf, 1) || 1;
  const size = fint(pf, 2) || items.length;
  const total = fint(pf, 3) || 0;
  const hasMore = pf.length ? !!fint(pf, 4) || cur * size < total : items.length > 0;
  return { items, page: cur, pageSize: size, total, hasMore };
}

async function listPage(page, pageSize = PAGE_SIZE) {
  const c = await client();
  let data;
  try {
    data = await c.call('GET', `/api/v1/video/list?page=${page}&page_size=${pageSize}`);
  } catch (e) {
    // 会话过期：重登一次再试
    resetClient();
    const c2 = await client();
    data = await c2.call('GET', `/api/v1/video/list?page=${page}&page_size=${pageSize}`);
    if (!data) throw e;
  }
  return decodePage(data);
}

/* ─────────────── 预览 → 签名清单 ─────────────── */

async function previewUrl(videoId) {
  const c = await client();
  const data = await c.call('GET', `/api/videos/${encodeURIComponent(videoId)}/preview`);
  const play = fbytes(fields(data), 5);
  const prim = fbytes(fields(play), 1);
  const url = fstr(fields(prim), 2);
  if (!url) throw new Error('videoshub 未返回预览地址: ' + videoId);
  return url;
}

function signFullPlaylist(preview, quality = DEFAULT_QUALITY) {
  const u = new URL(preview);
  const segs = u.pathname.split('/').filter(Boolean);
  const at = segs.indexOf('p2');
  if (at < 0) throw new Error('预览地址里没有 /p2/ 段: ' + preview);
  const [, video, pathLine] = segs.slice(at + 1, at + 4);
  const line = u.searchParams.get('line') || '';
  const ts = Math.floor(Date.now() / 1000) - 3;
  const nonce = hex(8);
  const s = mediaSign(video, line, quality, 'index.m3u8', ts, nonce);
  return {
    video,
    pathLine,
    line,
    quality,
    url: `${u.origin}/api/s3/p2/full/${video}/${pathLine}/${quality}/index.m3u8`
      + `?line=${encodeURIComponent(line)}&t=${ts}&n=${nonce}&s=${s}`,
  };
}

export async function resolvePlaylist(videoId, quality = DEFAULT_QUALITY) {
  const want = QUALITIES.includes(quality) ? quality : DEFAULT_QUALITY;
  const preview = await previewUrl(videoId);
  const signed = signFullPlaylist(preview, want);
  return { preview, ...signed };
}

/* ─────────────── 分片下载 + AES-128-CBC 解密 + 合并 ─────────────── */

async function fetchBytes(url, referer, retries = 3) {
  let last;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await fetchRaw(url, {
        headers: { 'User-Agent': UA, ...(referer ? { Referer: referer } : {}) },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return Buffer.from(await res.arrayBuffer());
    } catch (e) {
      last = e;
      if (attempt < retries) await new Promise((r) => setTimeout(r, Math.min(2 ** attempt * 500, 4000)));
    }
  }
  throw new Error(`fetch failed ${url}: ${last?.message || last}`);
}

function parsePlaylist(text) {
  const segs = [];
  let seq = 0;
  let key = null;
  let iv = null;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) {
      seq = Number(line.split(':')[1]) || 0;
      continue;
    }
    if (line.startsWith('#EXT-X-KEY:')) {
      const m = /URI="([^"]+)"/.exec(line);
      if (m) key = m[1];
      const ivm = /IV=0x([0-9a-fA-F]+)/.exec(line);
      iv = ivm ? Buffer.from(ivm[1].padStart(32, '0'), 'hex') : null;
      continue;
    }
    if (line.startsWith('#')) continue;
    if (key) segs.push({ name: line, key, iv: iv || intToBuf(seq) });
    seq += 1;
  }
  return segs;
}

function intToBuf(n) {
  const b = Buffer.alloc(16);
  b.writeUInt32BE(Number(n) >>> 0, 12);
  return b;
}

function pkcs7Unpad(buf) {
  if (!buf.length) return buf;
  const pad = buf[buf.length - 1];
  if (pad < 1 || pad > 16 || pad > buf.length) return buf;
  return buf.subarray(0, buf.length - pad);
}

function decryptSegment(key, iv, data) {
  const d = crypto.createDecipheriv('aes-128-cbc', key, iv);
  return pkcs7Unpad(Buffer.concat([d.update(data), d.final()]));
}

function which(bin) {
  const exts = (process.env.PATHEXT || '.EXE;.CMD;.BAT').split(';');
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of ['', ...exts]) {
      const full = path.join(dir, bin + ext);
      try {
        if (fs.existsSync(full)) return full;
      } catch { /* ignore */ }
    }
  }
  return null;
}

export function hasFfmpeg() {
  return !!which('ffmpeg');
}

function remux(tsPath, mp4Path) {
  const ffmpeg = which('ffmpeg');
  if (!ffmpeg) return Promise.resolve(false);
  return new Promise((resolve) => {
    const proc = spawn(ffmpeg, ['-y', '-loglevel', 'error', '-i', tsPath, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', mp4Path]);
    proc.on('error', () => resolve(false));
    proc.on('close', (code) => {
      try {
        resolve(code === 0 && fs.statSync(mp4Path).size > 0);
      } catch {
        resolve(false);
      }
    });
  });
}

/**
 * 下载整片：并发取分片（顺序落盘，TS 不会错乱）+ AES-128-CBC 解密 + 可选转 MP4。
 * @returns {Promise<{file:string, bytes:number, segments:number, remuxed:boolean, outdir:string}>}
 */
export async function downloadAndMerge(videoId, opts = {}) {
  const {
    quality = DEFAULT_QUALITY,
    maxSegments = 0,
    workers = 8,
    outName = null,
    keepTs = true,
    makeMp4 = true,
    outdir = path.join(os.tmpdir(), 'swipefeed-videoshub'),
    onProgress = null,
  } = opts;

  const { url: fullUrl } = await resolvePlaylist(videoId, quality);
  const playlistText = (await fetchBytes(fullUrl, undefined)).toString('utf8');
  const segs = parsePlaylist(playlistText);
  if (!segs.length) throw new Error('清单里没有可下载的分片（可能是 master playlist）');

  const total = maxSegments > 0 ? Math.min(segs.length, maxSegments) : segs.length;
  fs.mkdirSync(outdir, { recursive: true });
  const base = fullUrl.replace(/[^/]*$/, '');
  const name = outName || `videoshub-${videoId}-${quality}`;
  const tsPath = path.join(outdir, `${name}.ts`);

  const keyCache = new Map();
  const abs = (u) => (/^https?:/i.test(u) ? u : base + u);

  const fetchOne = async (index) => {
    const seg = segs[index];
    const keyUrl = abs(seg.key);
    let key = keyCache.get(keyUrl);
    if (!key) {
      key = await fetchBytes(keyUrl, fullUrl);
      keyCache.set(keyUrl, key);
    }
    if (key.length !== 16) throw new Error(`AES key 长度异常 (${key.length}) : ${keyUrl}`);
    const enc = await fetchBytes(abs(seg.name), fullUrl);
    return { index, data: decryptSegment(key, seg.iv, enc), encBytes: enc.length };
  };

  // 并发取、严格按 index 顺序写盘（与上游实现一致）
  const fd = fs.openSync(tsPath, 'w');
  let written = 0;
  const pending = new Map();
  let next = 0;
  const limit = Math.max(1, Math.min(32, workers));
  const inFlight = new Set();

  const launch = () => {
    while (inFlight.size < limit && next < total) {
      const idx = next++;
      const p = fetchOne(idx)
        .then((r) => { pending.set(r.index, r); })
        .catch((e) => { pending.set(idx, { index: idx, error: e }); })
        .finally(() => { inFlight.delete(p); });
      inFlight.add(p);
    }
  };

  try {
    launch();
    while (written < total) {
      if (inFlight.size) await Promise.race([...inFlight]);
      while (pending.has(written)) {
        const r = pending.get(written);
        pending.delete(written);
        if (r.error) throw r.error;
        fs.writeSync(fd, r.data);
        written += 1;
        if (onProgress && (written % 10 === 0 || written === total)) onProgress(written, total, r.encBytes);
      }
      launch();
    }
  } finally {
    fs.closeSync(fd);
  }

  const tsBytes = fs.statSync(tsPath).size;
  if (!makeMp4) return { file: tsPath, bytes: tsBytes, segments: written, remuxed: false, outdir };

  const mp4Path = path.join(outdir, `${name}.mp4`);
  const ok = await remux(tsPath, mp4Path);
  if (!ok) return { file: tsPath, bytes: tsBytes, segments: written, remuxed: false, outdir };
  if (!keepTs) {
    try { fs.unlinkSync(tsPath); } catch { /* ignore */ }
  }
  const bytes = fs.statSync(mp4Path).size;
  return { file: mp4Path, bytes, segments: written, remuxed: true, outdir };
}

/* ─────────────── SiteSource 插件接口 ─────────────── */

export const videoshub = {
  id: 'videoshub',
  name: 'VideosHub',
  displayName: 'VideosHub 视频库',

  async nextPage(cursor) {
    const page = Number(cursor?.page) || 1;
    const res = await listPage(page);
    const items = (res.items || []).filter((c) => !cursor?.blocked?.includes(c.id));
    const hasMore = res.hasMore !== false && items.length > 0;
    return {
      items,
      nextCursor: hasMore ? { page: page + 1 } : null,
      hasMore,
      total: res.total || undefined,
      _httpStatus: 200,
    };
  },

  async search(q, cursor) {
    const keyword = q.trim().toLowerCase();
    if (!keyword) return { items: [], nextCursor: null, hasMore: false };
    // 接口没有搜索：按页拉取后本地过滤（标题 / id）
    const pages = Number(cursor?.pages) || 3;
    const hit = [];
    for (let p = 1; p <= pages; p++) {
      // eslint-disable-next-line no-await-in-loop
      const { items } = await listPage(p);
      if (!items.length) break;
      for (const it of items) {
        if ((it.title || '').toLowerCase().includes(keyword) || String(it.id) === keyword) hit.push(it);
      }
      if (hit.length >= 24) break;
    }
    return { items: hit, nextCursor: null, hasMore: false, note: 'client-side-filter' };
  },

  async categoryPage(category, cursor) {
    // 没有分类接口；空分类等同首页
    const _ = category;
    return this.nextPage(cursor);
  },

  async categories() {
    return [
      { id: 'all', displayName: '全部' },
      { id: '720p', displayName: '720P 直取' },
      { id: '1080p', displayName: '1080P 直取' },
    ];
  },

  async resolve(card) {
    const quality = card._quality || DEFAULT_QUALITY;
    const { url, preview } = await resolvePlaylist(card.id, quality);
    return {
      url,
      type: 'hls',
      qualities: QUALITIES.map((q) => ({ label: q, url: q === quality ? url : undefined })).filter((x) => x.url),
      cover: card.cover || null,
      duration: card.duration || null,
      note: 'videoshub-signed',
      referer: preview,
      expiresHintMs: Date.now() + 20 * 60_000,
    };
  },

  /** 下载入口（供 /api/download 用） */
  download: (id, opts) => downloadAndMerge(id, opts),
  resolvePlaylist,
  hasFfmpeg,
};
