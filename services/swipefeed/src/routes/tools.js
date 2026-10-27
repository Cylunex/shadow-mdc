/**
 * /api/tools* — `tool\` 素材区的工具目录接口。
 *
 *   GET /api/tools                       全部工具（?kind=userscript|text|archive|binary）
 *   GET /api/tools/capabilities          能力聚合统计
 *   GET /api/tools/sites                 tool 站点 × 处理方式 对照表（查看页用）
 *   GET /api/tools/sites/:id             单个站点条目
 *   GET /api/tools/:id                   单个工具详情（?refresh=1 强制重扫）
 */
import express from 'express';
import {
  listTools, getTool, getToolCatalog, capabilitiesSummary,
} from '../util/tools-catalog.js';
import { listToolSites, getToolSite, toolSiteSummary } from '../util/tool-sites.js';

export function createToolsRouter() {
  const router = express.Router();

  router.get('/', (req, res) => {
    try {
      const kind = req.query.kind ? String(req.query.kind) : null;
      const force = String(req.query.refresh || '') === '1';
      const payload = listTools({ kind, force });
      payload.capabilities = capabilitiesSummary();
      res.json(payload);
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  router.get('/capabilities', (_req, res) => {
    try {
      res.json({ capabilities: capabilitiesSummary() });
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  // 站点对照表：按是否服务端可直连分组，查看页据此决定出卡片墙还是出索引。
  router.get('/sites', (_req, res) => {
    try {
      res.json({
        summary: toolSiteSummary(),
        sites: listToolSites(),
      });
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  router.get('/sites/:id', (req, res) => {
    const site = getToolSite(String(req.params.id));
    if (!site) return res.status(404).json({ error: 'unknown tool site' });
    res.json({ site });
  });

  router.get('/meta', (_req, res) => {
    const cat = getToolCatalog();
    res.json({
      scannedAt: cat.scannedAt,
      roots: cat.roots,
      total: cat.tools.length,
      kinds: cat.tools.reduce((acc, t) => {
        acc[t.kind] = (acc[t.kind] || 0) + 1;
        return acc;
      }, {}),
    });
  });

  router.get('/:id', (req, res) => {
    try {
      const tool = getTool(String(req.params.id));
      if (!tool) return res.status(404).json({ error: 'unknown tool' });
      res.json({ tool });
    } catch (e) {
      res.status(500).json({ error: e.message || String(e) });
    }
  });

  return router;
}
