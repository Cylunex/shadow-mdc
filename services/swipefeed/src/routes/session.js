/**
 * /api/session* — 站点会话（cookie / localStorage）的导入与管理。
 *
 *   GET    /api/session                 列出已导入的会话（值不回传，只回长度/键名）
 *   POST   /api/session                 导入/更新一条（本机来源限定）
 *   DELETE /api/session/:host           删掉某站（/api/session/all 清空）
 *   GET    /api/session/check?url=      检查某个地址会不会带上会话
 *   GET    /api/session/userscript      下载配套油猴脚本（浏览器里一键导出会话）
 */
import express from 'express';
import {
  listSessions, saveSession, deleteSession, sessionFor, isLocalRequest, sessionFilePath,
} from '../util/session-store.js';
import { buildSessionUserscript } from './userscript.js';

export function createSessionRouter() {
  const router = express.Router();

  router.get('/', (_req, res) => {
    res.json({
      file: sessionFilePath(),
      sessions: listSessions(),
    });
  });

  router.post('/', (req, res) => {
    if (!isLocalRequest(req)) {
      return res.status(403).json({ error: '只接受本机来源（SWIPEFEED_ALLOW_REMOTE_SESSION=1 可放开）' });
    }
    try {
      const body = req.body || {};
      const host = body.host || (body.referer ? new URL(body.referer).hostname : '');
      const saved = saveSession(host, body);
      res.json({
        ok: true,
        host: saved.host,
        updatedAt: saved.updatedAt,
        cookieLen: (saved.cookie || '').length,
        storageKeys: (saved.storage || []).map((x) => x.key),
      });
    } catch (e) {
      res.status(e.status || 500).json({ error: e.message || String(e) });
    }
  });

  router.delete('/:host', (req, res) => {
    if (!isLocalRequest(req)) return res.status(403).json({ error: '只接受本机来源' });
    try {
      const host = String(req.params.host);
      const out = host === 'all' ? deleteSession(null) : deleteSession(host);
      res.json({ ok: true, ...out });
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  /** 检查某个 URL 会不会带上会话（不发起请求）。 */
  router.get('/check', (req, res) => {
    try {
      const url = String(req.query.url || '');
      const u = new URL(url);
      const s = sessionFor(u.hostname);
      res.json({
        host: u.hostname,
        hasSession: !!s,
        matchedHost: s ? s.host : null,
        cookieLen: s ? (s.cookie || '').length : 0,
        storageKeys: s && Array.isArray(s.storage) ? s.storage.map((x) => x.key) : [],
        stale: s ? !!s.stale : null,
      });
    } catch {
      res.status(400).json({ error: 'url 不合法' });
    }
  });

  router.get('/userscript', (req, res) => {
    const origin = String(req.query.origin || `${req.protocol}://${req.get('host')}`);
    const keys = String(req.query.keys || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const body = buildSessionUserscript({ origin, keys });
    res.set('Content-Type', 'text/plain; charset=utf-8');
    res.set('Content-Disposition', 'inline; filename="swipefeed-session.user.js"');
    res.send(body);
  });

  return router;
}
