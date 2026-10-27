/**
 * tools-catalog.js — 把本地 `tool\` 素材区整理成可查询的工具目录。
 *
 * 数据来源：F:\Freedom\tool（APK / 油猴脚本 / 规则包 / 样本）。
 * 目录里既有 `*.user.txt` 用户脚本原文，也有打包的 zip（zip 只登记，不在这里解包）。
 * 本模块只做“元数据 + 平台能力”整理，供 /api/tools 系列接口使用；
 * 与站点直连无关，任何站点接入仍然在 src/plugins/ 下单独实现。
 */
import fs from 'fs';
import path from 'path';

/** 默认扫描根；可用 SWIPEFEED_TOOL_DIRS 覆盖（分号分隔）。 */
const DEFAULT_DIRS = [
  'F:\\Freedom\\tool',
  'F:\\Freedom\\.nas-tmp\\tool-extract-202602',
];

const CACHE_TTL_MS = 30_000;
const MAX_TEXT_BYTES = 4 * 1024 * 1024;
const USERSCRIPT_EXT = /\.(user\.js|user\.txt|user\.user\.js)$/i;

let cache = null;

function scanRoots() {
  const raw = process.env.SWIPEFEED_TOOL_DIRS;
  if (!raw) return DEFAULT_DIRS;
  return raw.split(/[;|]/).map((s) => s.trim()).filter(Boolean);
}

function walk(dir, out, depth = 0) {
  if (depth > 4) return;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (/^(node_modules|\.git)$/i.test(e.name)) continue;
      walk(full, out, depth + 1);
    } else if (e.isFile()) {
      out.push(full);
    }
  }
}

/* ───────────────────────── userscript 元数据 ───────────────────────── */

function parseHeader(text) {
  const block = /\/\/\s*==UserScript==([\s\S]*?)\/\/\s*==\/UserScript==/.exec(text);
  if (!block) return null;
  const meta = { name: null, namespace: null, version: null, description: null, author: null, license: null, runAt: null, homepage: null, downloadURL: null };
  const matches = [];
  const requires = [];
  const grants = [];
  const connects = [];
  for (const line of block[1].split('\n')) {
    const m = /^\/\/\s*@([A-Za-z:_-]+)\s+(.*)$/.exec(line.trim());
    if (!m) continue;
    const key = m[1].toLowerCase().replace(/:[a-z]{2}$/, '');
    const val = m[2].trim();
    if (!val) continue;
    switch (key) {
      case 'name': if (!meta.name) meta.name = val; break;
      case 'namespace': meta.namespace = val; break;
      case 'version': meta.version = val; break;
      case 'description': if (!meta.description) meta.description = val; break;
      case 'author': meta.author = val; break;
      case 'license': meta.license = val; break;
      case 'run-at': meta.runAt = val; break;
      case 'homepageurl': case 'supporturl': meta.homepage = meta.homepage || val; break;
      case 'downloadurl': meta.downloadURL = val; break;
      case 'match': case 'include': matches.push(val); break;
      case 'require': requires.push(val); break;
      case 'grant': grants.push(val); break;
      case 'connect': connects.push(val); break;
      default: break;
    }
  }
  return { meta, matches, requires, grants, connects };
}

/** 从 @match 值里抽出主机名（含通配符的保留原样）。 */
function hostsOf(matches) {
  const hosts = [];
  for (const m of matches || []) {
    let host = null;
    const abs = /^https?:\/\/([^/]+)/i.exec(m);
    if (abs) host = abs[1];
    else if (/^https?:\/\//i.test(m)) host = m.replace(/^https?:\/\//i, '').split('/')[0];
    else if (/\*?[\w-]+\.[a-z]{2,}/i.test(m)) host = m.replace(/^\*:\/\//, '').split('/')[0];
    if (!host) continue;
    host = host.replace(/^\*\.?/, '*.');
    if (!hosts.includes(host)) hosts.push(host);
  }
  return hosts;
}

/* ───────────────────────── 能力归类 ───────────────────────── */

const CAPABILITY_RULES = [
  { id: 'vip-video', label: 'VIP/付费片强开', re: /vip|会员|订阅|金币|钻石|付费|强开|解锁|省*看|free\s*vip|premium|unlock/i, mode: 'browser' },
  { id: 'vod-listing', label: '列表/详情抓取', re: /列表|排行|搜索|卡片|直达|index|list|ranking|feed/i, mode: 'server' },
  { id: 'hls-download', label: 'HLS/MP4 下载', re: /下载|download|m3u8downloader|hls\s*下载|合成|ffmpeg|mux\.js/i, mode: 'browser' },
  { id: 'ad-remove', label: '去广告/去弹窗', re: /去广告|拦截|弹窗|ad\s*block|webRequest/i, mode: 'server' },
  { id: 'blur-remove', label: '去模糊/解锁预览', re: /去模糊|模糊|blur/i, mode: 'browser' },
  { id: 'session-bypass', label: '会话/额度绕过', re: /限额|计数|trial|history|浏览次数|试看|120\s*秒|限制/i, mode: 'browser' },
  { id: 'idor', label: '越权/IDOR 直取', re: /idor|越权|uploader\s*uid|无鉴权|\/remote\//i, mode: 'browser' },
  { id: 'novel-download', label: '小说/文档下载', re: /小说|novel|章节|txt\b/i, mode: 'browser' },
  { id: 'cleanup-ui', label: '界面清理/增强', re: /增强|净化|精简|折叠|夜间|zhihu|知乎/i, mode: 'browser' },
];

function capabilitiesOf(text, meta) {
  const hay = [
    meta?.name, meta?.description, meta?.namespace,
    (text || '').slice(0, 4000),
  ].filter(Boolean).join(' ');
  const out = [];
  for (const rule of CAPABILITY_RULES) {
    if (rule.re.test(hay)) out.push({ id: rule.id, label: rule.label, mode: rule.mode });
  }
  if (!out.length) out.push({ id: 'other', label: '未归类', mode: 'browser' });
  return out;
}

/* ───────────────────────── 单文件 → 工具条目 ───────────────────────── */

function toolIdFrom(rel) {
  return rel
    .replace(/\.(user\.js|user\.txt|js|txt|zip|json|apk)$/i, '')
    .replace(/[^\w\u4e00-\u9fa5]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase() || 'tool';
}

function describeStatus(kind, caps) {
  if (kind === 'archive') return '仅登记（压缩包未解包）';
  if (kind === 'binary') return '仅登记（二进制样本）';
  if (kind === 'userscript') {
    return needsBrowser(caps)
      ? '浏览器侧脚本：本工具目录页只做索引与下载，运行仍需油猴扩展'
      : '脚本：可参考其解析逻辑移植为服务端源';
  }
  return '资料/规则文件';
}

function needsBrowser(caps) {
  return (caps || []).some((c) => c.mode === 'browser');
}

function subToolsOf(text) {
  // 六合一/多功能脚本内部模块（形如 “模块 1/6：xxx”）
  const out = [];
  for (const m of text.matchAll(/模块\s*\d+\s*\/\s*\d+\s*[:：]\s*([^\n*]{2,60})/g)) {
    const t = m[1].replace(/[（(].*$/, '').trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out.slice(0, 12);
}

function scanFile(full) {
  const ext = path.extname(full).toLowerCase();
  const base = path.basename(full);
  let stat;
  try { stat = fs.statSync(full); } catch { return null; }
  const kind = /\.apk$/i.test(full) ? 'binary'
    : /\.zip$/i.test(full) ? 'archive'
      : USERSCRIPT_EXT.test(full) ? 'userscript'
        : /\.js$/i.test(full) ? 'script'
          : 'text';

  const entry = {
    id: toolIdFrom(base),
    file: base,
    path: full,
    relPath: null,
    kind,
    origin: null,
    bytes: stat.size,
    mtime: stat.mtime.toISOString(),
    name: base.replace(/\.(user\.js|user\.txt|js|txt)$/i, ''),
    version: null,
    author: null,
    source: null,
    description: null,
    hosts: [],
    capabilities: [],
    modules: [],
    grants: [],
    connects: [],
    requires: [],
    runAt: null,
    status: '',
    inRepo: false,
    downloadPath: null,
  };

  // 只登记 tool\ 与解包工作区里的东西；解包依赖（*.min.js / node_modules）不算工具
  if (/[\\/]tool[\\/]/i.test(full) && !/[\\/]\.nas-tmp[\\/]/i.test(full)) entry.origin = 'tool-dir';
  else if (/[\\/]\.nas-tmp[\\/]/i.test(full)) entry.origin = 'extract-workspace';
  else entry.origin = 'other';
  if (/(\.min\.js|\.min\.css)$/i.test(base) || /node_modules/i.test(full)) {
    entry.status = '仅登记（依赖文件，不算工具）';
    entry.capabilities = [{ id: 'other', label: '依赖/产物', mode: 'browser' }];
    entry.kindDetected = 'dependency';
    return entry;
  }

  if (kind === 'archive' || kind === 'binary') {
    entry.description = kind === 'archive'
      ? '压缩包（内含油猴脚本/规则，见 README 的整合说明）'
      : '二进制样本，只登记不解析';
    entry.capabilities = [{ id: 'other', label: '素材样本', mode: 'browser' }];
    entry.status = describeStatus(kind, entry.capabilities);
    return entry;
  }

  if (stat.size > MAX_TEXT_BYTES) {
    entry.description = '超过 4MB，仅登记';
    entry.status = '仅登记（体积过大）';
    return entry;
  }

  // 大体积未加头的产物（站点打包 js 等）不当工具解析
  if (stat.size > 512 * 1024) {
    entry.description = '大于 512KB 的构建产物，仅登记';
    entry.status = '仅登记（构建产物）';
    entry.capabilities = [{ id: 'other', label: '构建产物', mode: 'browser' }];
    return entry;
  }

  let text = '';
  try { text = fs.readFileSync(full, 'utf8'); } catch { /* keep empty */ }

  const parsed = parseHeader(text);
  if (parsed) {
    const { meta, matches, requires, grants, connects } = parsed;
    entry.name = meta.name || entry.name;
    entry.version = meta.version;
    entry.author = meta.author;
    entry.description = meta.description;
    entry.source = meta.namespace || meta.homepage || null;
    entry.runAt = meta.runAt;
    entry.hosts = hostsOf(matches);
    entry.grants = [...new Set(grants)];
    entry.connects = [...new Set(connects)];
    entry.requires = [...new Set(requires)];
    entry.modules = subToolsOf(text);
    entry.capabilities = capabilitiesOf(text, meta);
    entry.status = describeStatus('userscript', entry.capabilities);
  } else {
    // 普通文本：第一行的注释/标题当描述
    const firstLine = text.split('\n').map((l) => l.trim()).filter(Boolean)[0] || '';
    entry.description = firstLine.replace(/^[/*#\s]+/, '').slice(0, 200) || null;
    entry.capabilities = capabilitiesOf(text.slice(0, 4000), { description: entry.description });
    entry.status = describeStatus('text', entry.capabilities);
  }
  return entry;
}

/* ───────────────────────── 目录构建 ───────────────────────── */

function build() {
  const roots = scanRoots();
  const files = [];
  for (const root of roots) walk(root, files);
  const tools = [];
  const seen = new Set();
  for (const full of files) {
    const entry = scanFile(full);
    if (!entry) continue;
    if (seen.has(entry.path)) continue;
    seen.add(entry.path);
    tools.push(entry);
  }
  // 同 id 冲突（不同目录同名文件）：保留第一个，其余加目录后缀
  const byId = new Map();
  for (const t of tools) {
    if (!byId.has(t.id)) { byId.set(t.id, t); continue; }
    let n = 2;
    while (byId.has(`${t.id}-${n}`)) n++;
    t.id = `${t.id}-${n}`;
    byId.set(t.id, t);
  }
  // 附属文件（options.json / _storage.json 这类纯数据）不是「工具」，标成 support，
  // 前端默认折叠，避免工具目录被数据文件塞满。
  for (const t of tools) {
    const isData = /\.(json|log|db)$/i.test(t.file) || /(_|^)storage\.json$/i.test(t.file);
    const noCap = !t.capabilities || !t.capabilities.length;
    if (isData && (noCap || /\.json$/i.test(t.file))) {
      t.support = true;
      t.status = t.status || '附属数据文件（不是脚本）';
    }
  }
  // 同名条目合并：保留能力更全的一条，其余记到 copies 里（工具目录里重复卡片的来源）
  const byName = new Map();
  const merged = [];
  for (const t of tools) {
    const key = String(t.name || t.file || '').toLowerCase();
    const prev = byName.get(key);
    if (!prev) {
      byName.set(key, t);
      t.copies = t.copies || [];
      merged.push(t);
      continue;
    }
    const better = (t.capabilities?.length || 0) > (prev.capabilities?.length || 0) ? t : prev;
    const worse = better === t ? prev : t;
    better.copies = [...(better.copies || []), ...(worse.copies || []), { path: worse.relPath || worse.path, kind: worse.kind }];
    if (better !== prev) {
      const at = merged.indexOf(prev);
      if (at >= 0) merged[at] = better;
      byName.set(key, better);
    }
  }
  return {
    scannedAt: new Date().toISOString(),
    roots,
    tools: merged.sort((a, b) => {
      const w = (x) => (x.support ? 3 : x.kind === 'userscript' ? 0 : x.kind === 'text' ? 1 : 2);
      return w(a) - w(b) || a.name.localeCompare(b.name, 'zh');
    }),
  };
}

export function getToolCatalog({ force = false } = {}) {
  if (!force && cache && Date.now() - cache.at < CACHE_TTL_MS) return cache.value;
  const value = build();
  cache = { at: Date.now(), value };
  return value;
}

/** 精简列表（不含长字段），用于 /api/tools。 */
export function listTools(opts = {}) {
  const cat = getToolCatalog(opts);
  const items = cat.tools.map((t) => ({
    id: t.id,
    name: t.name,
    file: t.file,
    kind: t.kind,
    origin: t.origin,
    bytes: t.bytes,
    mtime: t.mtime,
    version: t.version,
    author: t.author,
    description: t.description,
    hosts: t.hosts,
    capabilities: t.capabilities,
    modules: t.modules,
    status: t.status,
    downloadPath: t.downloadPath,
    support: !!t.support,
    copies: t.copies || [],
  }));
  const kind = opts.kind ? String(opts.kind) : null;
  const origin = opts.origin ? String(opts.origin) : null;
  let filtered = kind ? items.filter((t) => t.kind === kind) : items;
  if (origin) filtered = filtered.filter((t) => t.origin === origin);
  // 依赖文件默认不混进工具列表（除非显式 ?origin=extract-workspace 之类）
  if (!origin && String(opts.allDeps || '') !== '1') {
    filtered = filtered.filter((t) => !/依赖/.test(t.status));
  }
  return { scannedAt: cat.scannedAt, roots: cat.roots, total: filtered.length, items: filtered };
}

export function getTool(id) {
  if (!id) return null;
  const cat = getToolCatalog();
  return cat.tools.find((t) => t.id === id) || null;
}

/** 注册从 public/tools-support 暴露的可下载副本；返回已注册数量。 */
export function registerSupportFiles(files) {
  const cat = getToolCatalog();
  let n = 0;
  for (const t of cat.tools) {
    for (const f of files) {
      if (path.basename(f) === t.file) {
        t.inRepo = true;
        // 相对路径：经 /swipefeed 反代或直连根路径都能取到
        t.downloadPath = `tools-support/${encodeURIComponent(path.basename(f))}`;
        n++;
      }
    }
  }
  if (n) cache = { at: Date.now(), value: cat };
  return n;
}

export function capabilitiesSummary() {
  const cat = getToolCatalog();
  const map = new Map();
  for (const t of cat.tools) {
    for (const c of t.capabilities || []) {
      const cur = map.get(c.id) || { id: c.id, label: c.label, mode: c.mode, count: 0, tools: [] };
      cur.count++;
      if (cur.tools.length < 12) cur.tools.push(t.id);
      map.set(c.id, cur);
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count);
}
