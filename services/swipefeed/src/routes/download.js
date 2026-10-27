/**
 * /api/download* — 整片下载（移植 VideosHub 下载器的服务端版本）。
 *
 *   GET  /api/download/status?site=videoshub&id=32240
 *        探清单：分片数、总时长、加密方式、是否装了 ffmpeg（决定出 mp4 还是 ts）
 *   GET  /api/download?site=videoshub&id=32240&quality=720p&max=0&workers=8&mp4=1
 *        并发取分片 → AES-128-CBC 解密 → 顺序拼 .ts →（有 ffmpeg）-c copy 转 MP4
 *        完成后以附件形式回传，可选保留文件（?keep=1）
 *
 * 单次请求同步等待整片下载，属于“拉一份带走”的用法；长片建议带 &max=50 先试水。
 */
import express from 'express';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawn } from 'child_process';
import {
  resolvePlaylist, downloadAndMerge, hasFfmpeg, videoshub,
} from '../plugins/videoshub.js';
import { fetchRaw } from '../util/http.js';

const QUALITIES = ['720p', '1080p', '480p'];
const MAX_WORKERS = 16;

function parsePlaylistInfo(text) {
  const lines = text.split('\n').map((l) => l.trim());
  const segs = lines.filter((l) => l && !l.startsWith('#'));
  const durations = lines
    .filter((l) => l.startsWith('#EXTINF:'))
    .map((l) => parseFloat(l.slice(8)))
    .filter((n) => Number.isFinite(n));
  const keyLine = lines.find((l) => l.startsWith('#EXT-X-KEY:'));
  const key = keyLine ? (/METHOD=([A-Z0-9-]+)/.exec(keyLine) || [])[1] : null;
  return {
    segments: segs.length,
    durationSec: durations.length ? Math.round(durations.reduce((a, b) => a + b, 0)) : null,
    encryption: key || 'none',
    hasIndependentSegments: lines.some((l) => l.startsWith('#EXT-X-INDEPENDENT-SEGMENTS')),
  };
}

export function createDownloadRouter() {
  const router = express.Router();

  /** 探清单：不下载，只回报结构（用 max=1 的小请求都不需要）。 */
  router.get('/status', async (req, res) => {
    try {
      const id = String(req.query.id || '').trim();
      const quality = QUALITIES.includes(String(req.query.quality)) ? String(req.query.quality) : '720p';
      if (!id) return res.status(400).json({ error: 'id required' });
      const { url, preview, video, line } = await resolvePlaylist(id, quality);
      const playlist = await fetchRaw(url, { headers: { 'User-Agent': 'Mozilla/5.0', Referer: preview } });
      const text = await playlist.text();
      res.json({
        site: 'videoshub',
        id: video,
        line,
        quality,
        playlistUrl: url,
        httpStatus: playlist.status,
        cors: playlist.headers.get('access-control-allow-origin') || null,
        ...parsePlaylistInfo(text),
        ffmpeg: hasFfmpeg(),
        container: hasFfmpeg() ? 'mp4' : 'ts',
      });
    } catch (e) {
      res.status(502).json({ error: e.message || String(e) });
    }
  });

  /** 整片下载并回传。 */
  router.get('/', async (req, res) => {
    const started = Date.now();
    let out = null;
    try {
      const id = String(req.query.id || '').trim();
      if (!id) return res.status(400).json({ error: 'id required' });
      const quality = QUALITIES.includes(String(req.query.quality)) ? String(req.query.quality) : '720p';
      const maxSegments = Math.max(0, Math.min(5000, Number(req.query.max) || 0));
      const workers = Math.max(1, Math.min(MAX_WORKERS, Number(req.query.workers) || 8));
      const makeMp4 = String(req.query.mp4 ?? '1') !== '0';
      const keepTs = String(req.query.keep ?? '0') === '1';
      const keepFile = String(req.query.save ?? '0') === '1';

      const outdir = path.join(os.tmpdir(), 'swipefeed-videoshub');
      out = await downloadAndMerge(id, { quality, maxSegments, workers, makeMp4, keepTs, outdir });

      const name = `videoshub-${id}-${quality}${path.extname(out.file)}`;
      res.set('Content-Type', out.remuxed ? 'video/mp4' : 'video/mp2t');
      res.set('Content-Disposition', `attachment; filename="${name}"`);
      res.set('X-Segments', String(out.segments));
      res.set('X-Bytes', String(out.bytes));
      res.set('X-Took-Ms', String(Date.now() - started));
      res.sendFile(out.file, (err) => {
        if (err && !res.headersSent) res.status(500).end(String(err.message || err));
        if (!keepFile) {
          setTimeout(() => { try { fs.unlinkSync(out.file); } catch { /* ignore */ } }, 60_000);
        }
      });
    } catch (e) {
      res.status(502).json({ error: e.message || String(e), tookMs: Date.now() - started });
    }
  });

  /** 下载目录概览（?clean=1 清理）。 */
  router.get('/files', (req, res) => {
    const dir = path.join(os.tmpdir(), 'swipefeed-videoshub');
    try {
      if (!fs.existsSync(dir)) return res.json({ dir, files: [] });
      if (String(req.query.clean || '') === '1') {
        for (const f of fs.readdirSync(dir)) {
          try { fs.unlinkSync(path.join(dir, f)); } catch { /* ignore */ }
        }
        return res.json({ dir, files: [], cleaned: true });
      }
      const files = fs.readdirSync(dir).map((f) => {
        const st = fs.statSync(path.join(dir, f));
        return { name: f, bytes: st.size, mtime: st.mtime.toISOString() };
      });
      res.json({ dir, files });
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  return router;
}

export { videoshub };
