import express from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  listSites, listAllSites, getSource, CATEGORIES, mixedNextPage, searchAll,
  listToolBackedSources,
  categoryUrlFor,
} from './plugins/registry.js';
import { createToolsRouter } from './routes/tools.js';
import { createDownloadRouter } from './routes/download.js';
import { createSessionRouter } from './routes/session.js';
import { createMediaRouter, proxyUrl } from './routes/media.js';
import { discoverCategories } from './plugins/category-discovery.js';
import { browserInfo, browserQueueInfo } from './util/browser-fetch.js';
import { registerSupportFiles } from './util/tools-catalog.js';
import { listToolSites, getToolSite, toolSiteSummary } from './util/tool-sites.js';
import { fetchBuffer, unwrapPngPayload, getProxyStatus, fetchText } from './util/http.js';
import {
  listSiteSettings, updateSiteSettings, isSiteEnabled, reloadSiteSettings,
} from './util/site-settings.js';
import {
  recordOk, recordFail, getAllHealth, getSiteHealth, healthColor,
  resetCircuit, resetAllCircuits, isCircuitAllowing, CIRCUIT_CONSTANTS,
} from './util/circuit-breaker.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 8787);
const HOST = process.env.HOST || '0.0.0.0';

const app = express();
// 业务路由挂在 router 上，再同时挂到 "/" 与 "/swipefeed"：
// 前端 <base href="/swipefeed/">，直接访问 8787 与经反代挂到 /swipefeed 两种用法都能跑。
const apiRouter = express.Router();

const RESOLVE_CACHE_TTL_MS = 15 * 60 * 1000;
const resolveCache = new Map(); // key -> { stream, expires }

function getResolveCached(site, id) {
  const key = site + ':' + id;
  const hit = resolveCache.get(key);
  if (!hit) return null;
  if (Date.now() > hit.expires) {
    resolveCache.delete(key);
    return null;
  }
  return hit.stream;
}

function putResolveCached(site, id, stream) {
  resolveCache.set(site + ':' + id, {
    stream,
    expires: Date.now() + RESOLVE_CACHE_TTL_MS,
  });
  if (resolveCache.size > 500) {
    const now = Date.now();
    for (const [k, v] of resolveCache) {
      if (now > v.expires) resolveCache.delete(k);
    }
    if (resolveCache.size > 500) {
      const first = resolveCache.keys().next().value;
      resolveCache.delete(first);
    }
  }
}

app.use(express.json({ limit: '256kb' }));
// public 同时挂根路径与对外子路径（前端 <base href> 由这里决定）：
//   · NAS / 反向代理场景改 SWIPEFEED_BASE（如 /swipefeed、/nas/feed）即可，不用改代码
//   · 反代把前缀 strip 掉（proxy_pass 带尾斜杠）时，根路径挂载负责接住请求，
//     生成给浏览器的地址仍带 SWIPEFEED_BASE，两种接法都能用
const PUBLIC_DIR = path.join(__dirname, '..', 'public');
const PUBLIC_BASE = (() => {
  const raw = String(process.env.SWIPEFEED_BASE || '/swipefeed').trim();
  if (!raw || raw === '/') return '';
  const withSlash = raw.startsWith('/') ? raw : `/${raw}`;
  return withSlash.replace(/\/+$/, '');
})();

// index.html 里的 <base href> 跟着 SWIPEFEED_BASE 走
try {
  const indexHtml = fs.readFileSync(path.join(PUBLIC_DIR, 'index.html'), 'utf8');
  const baseHref = `${PUBLIC_BASE}/`;
  const serveIndex = (_req, res) => {
    res.type('html').send(indexHtml.replace(/<base href="[^"]*"/, `<base href="${baseHref}"`));
  };
  app.get('/', serveIndex);
  if (PUBLIC_BASE) app.get(PUBLIC_BASE + '/', serveIndex);
} catch { /* 没有 index.html 时忽略 */ }

app.use(express.static(PUBLIC_DIR));
if (PUBLIC_BASE) app.use(PUBLIC_BASE, express.static(PUBLIC_DIR));
// 工具目录副本（tool\ 里 *.user.txt，便于在查看页直接下载；不含 zip/apk）
app.use('/tools-support', express.static(path.join(PUBLIC_DIR, 'tools-support')));
if (PUBLIC_BASE) app.use(PUBLIC_BASE + '/tools-support', express.static(path.join(PUBLIC_DIR, 'tools-support')));
app.use('/api/tools', createToolsRouter());
// 下载 / 会话挂在 apiRouter 上：根路径与对外前缀两套都通
apiRouter.use('/api/download', createDownloadRouter());
apiRouter.use('/api/session', createSessionRouter());
apiRouter.use('/api', createMediaRouter());
// /swipefeed/api/*：与 base href 对齐的一套别名路由
const apiAliases = express.Router();
apiAliases.use('/api/tools', createToolsRouter());
if (PUBLIC_BASE) app.use(PUBLIC_BASE, apiAliases);
// 同一套业务路由两处挂载（根路径 + 对外前缀）
app.use('/', apiRouter);
if (PUBLIC_BASE) app.use(PUBLIC_BASE, apiRouter);

// 把 public/tools-support 下的脚本副本登记进工具目录（查看页的「下载脚本副本」链接）
try {
  const supportDir = path.join(PUBLIC_DIR, 'tools-support');
  const supportFiles = fs.existsSync(supportDir)
    ? fs.readdirSync(supportDir).map((f) => path.join(supportDir, f))
    : [];
  const registered = registerSupportFiles(supportFiles);
  if (registered) console.log(`tools-support: ${registered} 个脚本副本已登记`);
} catch (e) {
  console.warn('tools-support 登记失败:', e.message);
}

function enrichSitesWithHealth(rows) {
  return (rows || []).map((s) => {
    const h = getSiteHealth(s.id);
    return {
      ...s,
      health: {
        color: healthColor(s.id),
        state: h.state,
        healthScore: h.healthScore,
        avgLatencyMs: h.avgLatencyMs,
        success: h.success,
        fail: h.fail,
        consecutiveFails: h.consecutiveFails,
        autoBroken: h.autoBroken,
        coolDownRemainingMs: h.coolDownRemainingMs,
        lastError: h.lastError,
      },
    };
  });
}

apiRouter.get('/health', (_req, res) => {
  const sites = getAllHealth().map((h) => ({
    ...h,
    color: healthColor(h.site),
  }));
  res.json({
    ok: true,
    service: 'swipefeed-web',
    ts: Date.now(),
    proxy: getProxyStatus(),
    circuit: CIRCUIT_CONSTANTS,
    browser: { ...browserInfo(), queue: browserQueueInfo() },
    sites,
  });
});

apiRouter.get('/api/sites', (req, res) => {
  const includeDisabled = String(req.query.all || '') === '1';
  res.json({
    sites: listSites({ includeDisabled }),
    categories: CATEGORIES,
  });
});

/**
 * 查看页数据源：tool 站点 × 处理方式 + 服务端可直接出卡片墙的源。
 * GET /api/browse
 */
apiRouter.get('/api/browse', (_req, res) => {
  try {
    res.json({
      summary: toolSiteSummary(),
      sites: listToolSites(),
      serverSources: listToolBackedSources(),
      categories: CATEGORIES,
    });
  } catch (e) {
    res.status(500).json({ error: e.message || String(e) });
  }
});

apiRouter.get('/api/browse/:id', async (req, res) => {
  const site = getToolSite(String(req.params.id)) || { id: String(req.params.id) };
  const sourceId = site.sourceId || String(req.params.id);
  const src = getSource(sourceId);
  const force = String(req.query.refresh || '') === '1';
  // 分类动态解析：插件自带 API → 首页链接抽取 → 候选 slug × URL 形状探测（结果缓存 30 分钟）
  let discovery = null;
  try {
    discovery = await discoverCategories(sourceId, src, { force });
  } catch (e) {
    discovery = { site: sourceId, categories: [], source: 'error', note: e.message };
  }
  let categories = discovery?.categories?.length ? discovery.categories : null;
  if (!categories) {
    // 站点确实没有分类入口时，退回货架/排序这类总能用的入口（tool-sites 里配的）
    categories = (site.categories && site.categories.length)
      ? site.categories.map((c) => (typeof c === 'string' ? { id: c, displayName: c } : c))
      : [];
  }
  // 每个分类实际会请求的地址（前端挂到 chip 上，方便核对形状）
  let categoryUrls = {};
  if (src) {
    for (const c of categories.slice(0, 80)) {
      const u = categoryUrlFor(sourceId, c.id, 1);
      if (u) categoryUrls[c.id] = u;
    }
  }
  res.json({
    site: { ...site, sourceId },
    live: !!src,
    enabled: src ? isSiteEnabled(sourceId) : false,
    categories,
    categoryUrls,
    categorySource: discovery?.source || 'none',
    categoryPattern: discovery?.pattern || null,
    categoryNote: discovery?.note || null,
  });
});

/** 分类探测汇总：每个站点的分类是怎么来的（?refresh=1 强制重探，?site= 只查一个）。 */
apiRouter.get('/api/categories', async (req, res) => {
  const force = String(req.query.refresh || '') === '1';
  const only = req.query.site ? String(req.query.site) : null;
  const ids = only ? [only] : listSites({ includeDisabled: true }).map((s) => s.id);
  const rows = [];
  for (const id of ids) {
    if (id === 'demo') {
      rows.push({ site: id, source: 'demo', count: 0, categories: [] });
      continue;
    }
    try {
      // eslint-disable-next-line no-await-in-loop
      const d = await discoverCategories(id, getSource(id), { force });
      rows.push({
        site: id,
        source: d.source,
        pattern: d.pattern,
        note: d.note,
        count: d.categories.length,
        categories: d.categories.slice(0, 24),
      });
    } catch (e) {
      rows.push({ site: id, source: 'error', count: 0, note: e.message, categories: [] });
    }
  }
  res.json({
    total: rows.length,
    dynamic: rows.filter((r) => r.count > 0).length,
    browser: browserInfo(),
    none: rows.filter((r) => r.count === 0).map((r) => r.site),
    rows,
  });
});

/** Admin: full enable/disable map + circuit health. */
apiRouter.get('/api/site-settings', (_req, res) => {
  try {
    const payload = listSiteSettings(listAllSites());
    payload.sites = enrichSitesWithHealth(payload.sites);
    payload.circuit = CIRCUIT_CONSTANTS;
    res.json(payload);
  } catch (e) {
    res.status(500).json({ error: e.message || String(e) });
  }
});

apiRouter.post('/api/site-settings', (req, res) => {
  try {
    updateSiteSettings(req.body || {});
    // Re-enable also clears auto circuit when user turns a site back on
    const updates = req.body?.sites;
    const ids = Array.isArray(updates)
      ? updates.filter((r) => r && r.enabled).map((r) => r.id)
      : updates && typeof updates === 'object'
        ? Object.entries(updates)
          .filter(([, p]) => (p && typeof p === 'object' ? p.enabled !== false : !!p))
          .map(([id]) => id)
        : [];
    for (const id of ids) {
      const h = getSiteHealth(id);
      if (h.autoBroken || h.state === 'open' || h.state === 'half-open') {
        resetCircuit(id);
      }
    }
    const payload = listSiteSettings(listAllSites());
    payload.sites = enrichSitesWithHealth(payload.sites);
    payload.circuit = CIRCUIT_CONSTANTS;
    res.json(payload);
  } catch (e) {
    const status = e.status && e.status >= 400 ? e.status : 500;
    res.status(status).json({ error: e.message || String(e) });
  }
});

apiRouter.post('/api/site-settings/reload', (_req, res) => {
  try {
    reloadSiteSettings();
    const payload = listSiteSettings(listAllSites());
    payload.sites = enrichSitesWithHealth(payload.sites);
    res.json(payload);
  } catch (e) {
    res.status(500).json({ error: e.message || String(e) });
  }
});

/** Clear auto circuit for one site or all. */
apiRouter.post('/api/circuit-breaker/reset', (req, res) => {
  try {
    const { site, all } = req.body || {};
    if (all) {
      resetAllCircuits();
      return res.json({ ok: true, all: true, sites: getAllHealth() });
    }
    if (!site) return res.status(400).json({ error: 'site or all required' });
    const snap = resetCircuit(String(site));
    res.json({ ok: true, site: snap });
  } catch (e) {
    res.status(500).json({ error: e.message || String(e) });
  }
});

function rejectIfDisabled(site, res) {
  if (!site) return false;
  if (isSiteEnabled(site)) return false;
  res.status(403).json({
    error: 'site disabled',
    site,
    enabled: false,
    items: [],
    nextCursor: null,
    hasMore: false,
  });
  return true;
}

apiRouter.get('/api/feed', async (req, res) => {
  try {
    const mode = String(req.query.mode || 'single');
    const site = String(req.query.site || 'demo');
    const category = req.query.category ? String(req.query.category) : null;
    let cursor = null;
    if (req.query.cursor) {
      try { cursor = JSON.parse(String(req.query.cursor)); }
      catch { cursor = { page: Number(req.query.cursor) || 1 }; }
    }

    if (mode === 'random') {
      // 前端可指定参与混排的源：?sites=a,b,c（不传 = 全部启用源）
      const picked = String(req.query.sites || '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      const page = await mixedNextPage(cursor, { category: null, sites: picked.length ? picked : null });
      return res.json(stripInternal(page));
    }
    // 查看页：浏览网格（不带 demo 样例，按站点真实列表分页）
    if (mode === 'browse') {
      // 补标题/封面：部分站点列表只有 id（如 pektino），检查开关后由插件自己再抓详情页
      if (String(req.query.enrich || '') === '1') cursor = { ...(cursor || {}), enrich: true };
      if (site && site !== 'mixed' && site !== 'all') {
        if (rejectIfDisabled(site, res)) return;
        const src = getSource(site);
        if (!src) return res.status(404).json({ error: 'unknown site' });
        const t0 = Date.now();
        try {
          let page = null;
          if (category && category !== 'all') {
            // 先问插件自己（映射表/自带接口），再按该源登记或探测出的 URL 形状兜底
            try {
              page = await src.categoryPage(category, cursor);
            } catch { page = null; }
            if (!(page?.items || []).length) {
              const alt = await categoryFeedViaTemplate(src, category, cursor).catch(() => null);
              if (alt?.items?.length) page = alt;
            }
            if (!page) page = { items: [], nextCursor: null, hasMore: false };
          } else {
            page = await src.nextPage(cursor);
          }
          const items = page.items || [];
          if (items.length) recordOk(site, Date.now() - t0);
          else recordFail(site, 'empty page', Date.now() - t0);
          return res.json({ ...stripInternal(page), site, category: category || null });
        } catch (e) {
          recordFail(site, e.message || String(e), Date.now() - t0);
          throw e;
        }
      }
      const page = await mixedNextPage(cursor, { category: category && category !== 'all' ? category : null });
      return res.json({ ...stripInternal(page), site: 'mixed', category: category || null });
    }
    if (mode === 'category') {
      if (!category) return res.status(400).json({ error: 'category required' });
      if (site && site !== 'mixed' && site !== 'all') {
        if (rejectIfDisabled(site, res)) return;
        const src = getSource(site);
        if (!src) return res.status(404).json({ error: 'unknown site' });
        const t0 = Date.now();
        try {
          const page = await src.categoryPage(category, cursor);
          const items = page.items || [];
          if (items.length) recordOk(site, Date.now() - t0);
          else recordFail(site, 'empty page', Date.now() - t0);
          return res.json(stripInternal(page));
        } catch (e) {
          recordFail(site, e.message || String(e), Date.now() - t0);
          throw e;
        }
      }
      const page = await mixedNextPage(cursor, { category });
      return res.json(page);
    }
    // single
    if (rejectIfDisabled(site, res)) return;
    const src = getSource(site);
    if (!src) return res.status(404).json({ error: 'unknown site' });
    const t0 = Date.now();
    try {
      const page = await src.nextPage(cursor);
      const items = page.items || [];
      if (items.length) recordOk(site, Date.now() - t0);
      else recordFail(site, 'empty page', Date.now() - t0);
      return res.json(stripInternal(page));
    } catch (e) {
      recordFail(site, e.message || String(e), Date.now() - t0);
      throw e;
    }
  } catch (e) {
    const status = e.status && e.status >= 400 ? e.status : 502;
    res.status(status).json({
      error: e.message || String(e),
      items: [],
      nextCursor: null,
      hasMore: false,
    });
  }
});

apiRouter.get('/api/search', async (req, res) => {
  try {
    const q = String(req.query.q || '').trim();
    if (!q) return res.status(400).json({ error: 'q required' });
    let cursor = null;
    if (req.query.cursor) {
      try { cursor = JSON.parse(String(req.query.cursor)); }
      catch { cursor = { page: Number(req.query.cursor) || 1 }; }
    }
    const site = req.query.site ? String(req.query.site) : null;
    if (site) {
      if (rejectIfDisabled(site, res)) return;
      const src = getSource(site);
      if (!src) return res.status(404).json({ error: 'unknown site' });
      const t0 = Date.now();
      try {
        const page = await src.search(q, cursor);
        const items = page.items || [];
        if (items.length) recordOk(site, Date.now() - t0);
        else recordFail(site, 'empty search', Date.now() - t0);
        return res.json(stripInternal(page));
      } catch (e) {
        recordFail(site, e.message || String(e), Date.now() - t0);
        throw e;
      }
    }
    const page = await searchAll(q, cursor);
    return res.json(page);
  } catch (e) {
    res.status(502).json({ error: e.message || String(e), items: [], nextCursor: null, hasMore: false });
  }
});

apiRouter.post('/api/resolve', async (req, res) => {
  try {
    const { site, id, pageUrl } = req.body || {};
    if (!site || !id) return res.status(400).json({ error: 'site and id required' });
    if (rejectIfDisabled(site, res)) return;
    // Soft skip when circuit open (client may auto-next); allow half-open probe
    if (!isCircuitAllowing(site)) {
      return res.status(503).json({
        error: 'site circuit open',
        site,
        circuit: getSiteHealth(site),
      });
    }
    const src = getSource(site);
    if (!src) return res.status(404).json({ error: 'unknown site' });
    const cached = getResolveCached(site, id);
    if (cached) return res.json(cached);
    const card = { site, id, pageUrl: pageUrl || '', title: '', cover: null, duration: null, author: null, aspect: 'unknown' };
    const t0 = Date.now();
    try {
      const stream = await src.resolve(card);
      const ms = Date.now() - t0;
      if (stream?.url) {
        // 浏览器直接拉跨域媒体会被 ORB / Referer 拦（phncdn、3go 等），
        // 统一换成同源代理地址；上游地址保留在 directUrl 里供「复制直链」。
        const referer = card.pageUrl || stream.referer || '';
        const wrap = (raw, type) => proxyUrl(raw, { type, referer });
        const proxied = {
          ...stream,
          directUrl: /^https?:\/\//i.test(stream.url) ? stream.url : undefined,
          url: wrap(stream.url, stream.type),
          qualities: (stream.qualities || []).map((q) => ({
            ...q,
            directUrl: /^https?:\/\//i.test(q.url) ? q.url : undefined,
            url: wrap(q.url, stream.type),
          })),
        };
        putResolveCached(site, id, proxied);
        recordOk(site, ms);
        return res.json(proxied);
      }
      recordFail(site, 'empty stream', ms);
      return res.json(stream);
    } catch (e) {
      recordFail(site, e.message || String(e), Date.now() - t0);
      throw e;
    }
  } catch (e) {
    res.status(502).json({ error: e.message || String(e) });
  }
});

/**
 * Unwrap PNG-disguised HLS manifests/segments (rou.video).
 * 清单里的段（绝对或相对）统一按上游地址绝对化后回指本代理；
 * 代理前缀取当前挂载（req.baseUrl），根路径直连与 /swipefeed 反代都成立。
 */
apiRouter.get('/api/png-hls', async (req, res) => {
  try {
    const u = String(req.query.u || '');
    if (!/^https?:\/\//i.test(u)) return res.status(400).type('text').send('bad url');
    const referer = String(req.query.r || 'https://rou.video/');
    const browserApi = (() => {
      const raw = String(process.env.SWIPEFEED_BROWSER_API_PREFIX || '').trim();
      if (raw) return (raw.startsWith('/') ? raw : `/${raw}`).replace(/\/+$/, '');
      const base = (req.baseUrl || '').replace(/\/api\/png-hls$/, '');
      return `${base}/api`.replace(/\/+$/, '') || '/api';
    })();
    const proxyFor = (abs) =>
      `${browserApi}/png-hls?u=${encodeURIComponent(abs)}&r=${encodeURIComponent(referer)}`;
    const { status, buffer, contentType, finalUrl } = await fetchBuffer(u, { referer });
    if (status >= 400) return res.status(status).type('text').send(`upstream ${status}`);
    const unwrapped = await unwrapPngPayload(buffer);
    const outBuf = unwrapped || Buffer.from(buffer);
    const text = outBuf.toString('utf8');
    if (text.includes('#EXTM3U')) {
      const upstream = finalUrl || u;
      const rewritten = text.split('\n').map((line) => {
        const t = line.trim();
        if (!t || t.startsWith('#')) return line;
        let abs;
        try {
          abs = new URL(t, upstream).toString();
        } catch {
          return line;
        }
        // 段本身也要能过 png 解包（rou 的清单与分片都是 .png 伪装）
        return proxyFor(abs);
      }).join('\n');
      res.set('Cache-Control', 'no-store');
      return res.type('application/vnd.apple.mpegurl').send(rewritten);
    }
    res.set('Cache-Control', 'public, max-age=60');
    res.type(contentType.includes('mpeg') ? contentType : 'video/mp2t');
    return res.send(outBuf);
  } catch (e) {
    res.status(502).type('text').send(e.message || String(e));
  }
});

/**
 * 分类取数兜底：按该源登记的 URL 形状拼地址，再用源自己的解析函数取卡片。
 * 这样“动态探测到的任意 slug”都能过滤，而不只是写死映射表里那几个词。
 */
async function categoryFeedViaTemplate(src, slug, cursor) {
  const page = Number(cursor?.page) || 1;
  const url = categoryUrlFor(src.id, slug, page);
  if (!url) return null;
  const parse = src.parseListing;
  if (typeof parse !== 'function') return null;
  const reqOpts = { referer: src.base || url };
  let attempt = await fetchText(url, reqOpts);
  let items = parse(attempt.body) || [];
  if (!items.length && attempt.status >= 400) {
    // 少数站对移动 UA 直接 403/404，换桌面头再试一次
    const retry = await fetchText(url, { ...reqOpts, desktop: true, retry: false }).catch(() => null);
    if (retry) {
      const retryItems = parse(retry.body) || [];
      if (retryItems.length) {
        attempt = retry;
        items = retryItems;
      }
    }
  }
  const status = attempt.status;
  if (!items.length) {
    // 真的没内容才报错；有些站用 404 状态码给分类页（drtuber / anyporn 就是这样），
    // 只要解析出了卡片就算成功。
    if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status });
    return null;
  }
  return {
    items: items.map((c) => ({ ...c, site: src.id })),
    nextCursor: { page: page + 1 },
    hasMore: true,
    _httpStatus: status,
    note: 'category-template',
  };
}

function stripInternal(page) {
  const { _httpStatus, _raw, ...rest } = page || {};
  if (Array.isArray(rest.items)) {
    rest.items = rest.items.map((c) => {
      const { _media, _spider, _vodId, ...card } = c;
      return card;
    });
  }
  return rest;
}

/**
 * 图片代理：部分站点（如 media.3go.fun）带防盗链，直接热链会被浏览器拦。
 * 由服务端取回后按原类型回给查看页，避免封面全空。
 * GET /api/img?u=<encoded>
 *
 * 只允许公网 http(s) 图片源：解析 DNS 后拒环回/私网/链路本地，重定向手动跟随后再校验一次；
 * 单张上限 512KB，缓存带 TTL 并限条目数。
 */
const imgCache = new Map(); // url -> { buf, type, expires }
const IMG_TTL_MS = 10 * 60 * 1000;
const IMG_MAX_BYTES = 512 * 1024;
const IMG_CACHE_MAX = 200;

function isPrivateAddress(addr) {
  const a = String(addr || '').toLowerCase();
  if (!a) return true;
  if (a === '::1' || a === '::' || a.startsWith('fe80:') || a.startsWith('fc') || a.startsWith('fd')) return true;
  if (/^127\./.test(a) || /^10\./.test(a)) return true;
  if (/^192\.168\./.test(a)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(a)) return true;
  if (/^169\.254\./.test(a)) return true;
  if (/^0\./.test(a)) return true;
  return false;
}

/** 只放行公网 host（DNS 解析结果全部是公网地址才通过）。 */
async function assertPublicHost(hostname) {
  if (!hostname) throw new Error('bad host');
  if (/^(localhost|.*\.local|.*\.internal)$/i.test(hostname)) throw new Error('blocked host');
  if (/^\[?[0-9a-f:.]+\]?$/i.test(hostname) && isPrivateAddress(hostname.replace(/[\[\]]/g, ''))) {
    throw new Error('blocked host');
  }
  const dns = await import('dns');
  const addrs = await new Promise((resolve) => {
    dns.lookup(hostname, { all: true }, (err, list) => resolve(err ? [] : list));
  });
  if (!addrs.length) throw new Error('dns failed');
  for (const row of addrs) {
    if (isPrivateAddress(row.address)) throw new Error('blocked private address');
  }
}

function sweepImgCache() {
  const now = Date.now();
  for (const [k, v] of imgCache) if (now > v.expires) imgCache.delete(k);
  while (imgCache.size > IMG_CACHE_MAX) {
    const first = imgCache.keys().next().value;
    imgCache.delete(first);
  }
}

apiRouter.get('/api/img', async (req, res) => {
  try {
    const u = String(req.query.u || '');
    if (!/^https?:\/\//i.test(u)) return res.status(400).type('text').send('bad url');
    const hit = imgCache.get(u);
    if (hit && Date.now() < hit.expires) {
      res.set('Cache-Control', 'public, max-age=600');
      res.type(hit.type);
      return res.send(hit.buf);
    }
    let target;
    try {
      target = new URL(u);
    } catch {
      return res.status(400).type('text').send('bad url');
    }
    await assertPublicHost(target.hostname);

    const referer = String(req.query.r || target.origin + '/');
    // 死图（CDN 522 / 超时）不能拖住浏览器：5 秒放弃 + 90 秒负缓存 + 直接 204。
    // 否则一屏 20 多张死图会占满每主机 6 条连接，连 hls.js 都加载不出来。
    let fetched;
    try {
      fetched = await fetchBuffer(u, { referer, timeout: 4000, redirect: 'manual' });
    } catch {
      imgCache.set(u, { buf: Buffer.alloc(0), type: 'image/gif', expires: Date.now() + 90_000 });
      res.set('Cache-Control', 'public, max-age=90');
      return res.status(204).end();
    }
    const { status, buffer, contentType, finalUrl } = fetched;
    if (status >= 300 && status < 400) return res.status(502).type('text').send('redirect blocked');
    if (status >= 400) {
      imgCache.set(u, { buf: Buffer.alloc(0), type: 'image/gif', expires: Date.now() + 90_000 });
      res.set('Cache-Control', 'public, max-age=90');
      return res.status(204).end();
    }
    if (finalUrl && finalUrl !== u) {
      try { await assertPublicHost(new URL(finalUrl).hostname); } catch {
        return res.status(502).type('text').send('redirect blocked');
      }
    }
    const type = /^image\//i.test(contentType) ? contentType : 'image/jpeg';
    const buf = Buffer.from(buffer);
    if (buf.length > IMG_MAX_BYTES) {
      res.set('Cache-Control', 'public, max-age=60');
      res.type(type);
      return res.send(buf);
    }
    imgCache.set(u, { buf, type, expires: Date.now() + IMG_TTL_MS });
    sweepImgCache();
    res.set('Cache-Control', 'public, max-age=600');
    res.type(type);
    return res.send(buf);
  } catch (e) {
    res.status(502).type('text').send(e.message || String(e));
  }
});

app.listen(PORT, HOST, () => {
  console.log(`swipefeed-web listening on http://${HOST}:${PORT} proxy=${getProxyStatus()}`);
});
