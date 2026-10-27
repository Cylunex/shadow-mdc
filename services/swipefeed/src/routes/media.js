/**
 * /api/hls、/api/stream — 本地媒体代理。
 *
 * 为什么需要：很多站的 CDN 只在“带对 Referer / 同源”时才给字节。
 *   · phncdn 的分片直接 403 / ERR_FAILED（要 pornhub 的 Referer）
 *   · media.3go.fun 的原画被 Chrome 的 ORB 拦（跨域 + MIME 不明确）
 * 服务端取数时我们能带 Referer / UA，所以把媒体拉回本地再喂给播放器：
 *   · 播放器只跟 127.0.0.1 打交道 → 没有跨域、没有 ORB、没有 Referer 问题
 *   · 代理侧补上正确的 Referer、会话 Cookie（用户导入过的话）、Range 透传
 *
 * 安全边界：只允许 http(s)、只做“取回并转发”，不解析、不改写内容（HLS 清单除外，
 * 需要把分片地址改回本代理；不解密、不绕过任何鉴权）。
 */
import express from 'express';
import { Readable } from 'stream';
import { pipeline } from 'stream/promises';
import { fetchRaw } from '../util/http.js';
import {
  buildPlaylist, takeKey,
} from '../util/hj-key.js';

const DEFAULT_TIMEOUT = 30000;
/** 对外子路径（与 server.js 的 SWIPEFEED_BASE 一致）；反代 strip 前缀时这里生成给浏览器的地址仍然要对 */
const PUBLIC_BASE = (() => {
  const raw = String(process.env.SWIPEFEED_BASE || '/swipefeed').trim();
  if (!raw || raw === '/') return '';
  return (raw.startsWith('/') ? raw : '/' + raw).replace(/\/+$/, '');
})();
/**
 * Browser-facing API root (no trailing slash). Default `${SWIPEFEED_BASE}/api`.
 * Inside shadow-mdc set SWIPEFEED_BROWSER_API_PREFIX=/api/swipe so playlist
 * rewrites and resolve wrappers point at FastAPI's reverse proxy.
 */
const BROWSER_API_ROOT = (() => {
  const raw = String(process.env.SWIPEFEED_BROWSER_API_PREFIX || `${PUBLIC_BASE}/api`).trim();
  if (!raw || raw === '/') return '/api';
  return (raw.startsWith('/') ? raw : `/${raw}`).replace(/\/+$/, '');
})();

function isHttp(u) {
  return /^https?:\/\//i.test(u || '');
}

/**
 * 取上游响应：只对“拿到响应头”计时；开始吐字节后就不再计时。
 * 之前用 AbortSignal.timeout 覆盖整个 body，长视频会在 30s 处被 abort，
 * 并触发 Readable 的未处理 error 事件把进程带崩。
 */
async function openUpstream(url, headers) {
  // 媒体走 identity：既避免长度不符，也省掉解压开销
  const hdrs = { 'Accept-Encoding': 'identity', ...headers };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), DEFAULT_TIMEOUT);
  try {
    return await fetchRaw(url, { headers: hdrs, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

/** 清单里所有 URI（分片、子清单、KEY、MAP、MEDIA）都改回本代理。 */
function rewritePlaylist(text, upstreamUrl, prefix, referer) {
  const wrap = (abs) =>
    `${prefix}/hls?u=${encodeURIComponent(abs)}&r=${encodeURIComponent(referer || '')}`;
  const resolve = (raw) => {
    try {
      return new URL(raw, upstreamUrl).toString();
    } catch {
      return null;
    }
  };
  const out = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) {
      out.push(raw);
      continue;
    }
    if (line.startsWith('#')) {
      out.push(
        raw.replace(/URI="([^"]+)"/g, (m, uri) => {
          const abs = resolve(uri);
          return abs ? `URI="${wrap(abs)}"` : m;
        }),
      );
    } else {
      const abs = resolve(line);
      out.push(abs ? wrap(abs) : raw);
    }
  }
  return out.join('\n');
}

function passThroughHeaders(res, upstream, extra = {}) {
  const ct = upstream.headers.get('content-type') || '';
  // 上游若带 Content-Encoding，Content-Length 是压缩后的长度，转发会长度不符
  const encoded = !!upstream.headers.get('content-encoding');
  const len = encoded ? null : upstream.headers.get('content-length');
  const range = upstream.headers.get('content-range');
  const acceptRanges = upstream.headers.get('accept-ranges');
  if (ct) res.set('Content-Type', ct);
  if (len) res.set('Content-Length', len);
  if (range) res.set('Content-Range', range);
  if (acceptRanges) res.set('Accept-Ranges', acceptRanges);
  for (const [k, v] of Object.entries(extra)) if (v) res.set(k, v);
}

async function pipeUpstream(req, res, url, referer) {
  const headers = {};
  if (req.headers.range) headers.Range = req.headers.range;
  if (referer) headers.Referer = referer;
  const upstream = await openUpstream(url, headers);
  if (upstream.status >= 400) {
    res.status(upstream.status).type('text').send(`upstream ${upstream.status}`);
    return;
  }
  passThroughHeaders(res, upstream, {
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
  });
  res.status(upstream.status);
  if (req.method === 'HEAD' || !upstream.body) {
    res.end();
    return;
  }
  // 用 pipeline 接错误：客户端中途断开（拖动进度条）不会变成未捕获异常
  const source = Readable.fromWeb(upstream.body);
  req.on('close', () => {
    try { source.destroy(); } catch { /* ignore */ }
  });
  try {
    await pipeline(source, res);
  } catch {
    try { res.destroy(); } catch { /* ignore */ }
  }
}

export function createMediaRouter() {
  const router = express.Router();

  /**
   * HLS：清单就地改写，分片/密钥原样转发（含 Range）。
   * GET /api/hls?u=<encoded>&r=<referer>
   */
  router.get('/hls', async (req, res) => {
    const u = String(req.query.u || '');
    const referer = String(req.query.r || '');
    if (!isHttp(u)) return res.status(400).type('text').send('bad url');
    try {
      const headers = {};
      if (referer) headers.Referer = referer;
      if (req.headers.range) headers.Range = req.headers.range;
      const upstream = await openUpstream(u, headers);
      if (upstream.status >= 400) {
        return res.status(upstream.status).type('text').send(`upstream ${upstream.status}`);
      }
      const ct = (upstream.headers.get('content-type') || '').toLowerCase();
      const buf = Buffer.from(await upstream.arrayBuffer());
      const head = buf.subarray(0, 7).toString('utf8');
      const looksPlaylist = ct.includes('mpegurl') || head === '#EXTM3U';
      if (!looksPlaylist) {
        // 分片 / 密钥：原样回传
        passThroughHeaders(res, upstream, {
          'Cache-Control': 'no-store',
          'Access-Control-Allow-Origin': '*',
        });
        return res.status(upstream.status).send(buf);
      }
      const rewritten = rewritePlaylist(buf.toString('utf8'), u, BROWSER_API_ROOT, referer);
      res.set('Content-Type', 'application/vnd.apple.mpegurl');
      res.set('Cache-Control', 'no-store');
      res.set('Access-Control-Allow-Origin', '*');
      return res.send(rewritten);
    } catch (e) {
      return res.status(502).type('text').send(e.message || String(e));
    }
  });

  /**
   * 直接 MP4 / TS：流式转发，支持 Range（可拖动）。
   * GET /api/stream?u=<encoded>&r=<referer>
   */
  router.get('/stream', async (req, res) => {
    const u = String(req.query.u || '');
    const referer = String(req.query.r || '');
    if (!isHttp(u)) return res.status(400).type('text').send('bad url');
    try {
      await pipeUpstream(req, res, u, referer);
    } catch (e) {
      if (!res.headersSent) res.status(502).type('text').send(e.message || String(e));
      else res.end();
    }
  });

  /**
   * 海角专用 HLS：清单里的 `.key` 解不开，需要先合成出真正的 AES 密钥，
   * 再把 KEY 行指向本地密钥端点（密钥本体只存在进程内，不写进 URL）。
   * GET /api/hj-hls?u=<encoded>
   */
  router.get('/hj-hls', async (req, res) => {
    const u = String(req.query.u || '');
    if (!isHttp(u)) return res.status(400).type('text').send('bad url');
    try {
      const built = await buildPlaylist(u, BROWSER_API_ROOT);
      if (!built) return res.status(502).type('text').send('海角密钥合成失败（清单/素材/分片不可用）');
      res.set('Content-Type', 'application/vnd.apple.mpegurl');
      res.set('Cache-Control', 'no-store');
      res.set('Access-Control-Allow-Origin', '*');
      return res.send(built.playlist);
    } catch (e) {
      return res.status(502).type('text').send(e.message || String(e));
    }
  });

  /** 合成后的 16 字节密钥（给上面那份清单用）。GET /api/hj-key?t=<token> */
  router.get('/hj-key', (req, res) => {
    const key = takeKey(req.query.t);
    if (!key) return res.status(404).type('text').send('key expired');
    res.set('Content-Type', 'application/octet-stream');
    res.set('Cache-Control', 'no-store');
    res.set('Access-Control-Allow-Origin', '*');
    return res.send(key);
  });

  return router;
}

/** 把播放地址换成同源代理地址；已是本地地址的原样返回。 */
export function proxyUrl(rawUrl, { type = '', referer = '' } = {}) {
  if (!rawUrl) return rawUrl;
  if (!isHttp(rawUrl)) return rawUrl; // 已经是 /swipefeed/api/... 之类的本地代理
  const kind = type === 'hls' || /\.m3u8/i.test(rawUrl) ? 'hls' : 'stream';
  return `${BROWSER_API_ROOT}/${kind}?u=${encodeURIComponent(rawUrl)}&r=${encodeURIComponent(referer || '')}`;
}
