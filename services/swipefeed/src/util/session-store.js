/**
 * session-store.js — 站点会话（cookie / localStorage）的本地保管。
 *
 * 用途：hstv / caoliu / xasian / javhub … 这些站在服务端取源前需要“你浏览器里那份身份”，
 * 但服务端没有你的浏览器。这里提供一个明确、可控的通道：
 *
 *   1) 浏览器里装配套油猴脚本（/api/session/userscript），在目标站上点一下「导出会话」，
 *      自动 POST 到本服务；或
 *   2) 在查看页「会话」面板里手动粘贴 Cookie / localStorage 片段；
 *   3) 服务端按域名把这些头带到该站的请求上（只对登记过的域名生效）。
 *
 * 安全边界（重要）：
 *   · 只落本地 `data/sessions.json`（已在 .gitignore 里排除 data/ 之外的敏感项，注意不要提交）；
 *   · 只允许 http(s)、只对请求域名精确匹配或子域匹配时才附带；
 *   · 接口默认只接受本机来源（127.0.0.1 / localhost），避免被局域网随意写入；
 *   · 可随时 DELETE 清空；值不会进日志。
 */
import fs from 'fs';
import path from 'path';

const DATA_DIR = process.env.SWIPEFEED_DATA_DIR || path.join(process.cwd(), 'data');
const FILE = path.join(DATA_DIR, 'sessions.json');
const MAX_AGE_MS = Number(process.env.SWIPEFEED_SESSION_TTL_HOURS || 24 * 7) * 3600 * 1000;

let cache = null;

function ensureDir() {
  try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch { /* ignore */ }
}

function load() {
  if (cache) return cache;
  ensureDir();
  try {
    const raw = fs.readFileSync(FILE, 'utf8');
    const parsed = JSON.parse(raw);
    cache = parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    cache = {};
  }
  return cache;
}

function persist() {
  ensureDir();
  try {
    fs.writeFileSync(FILE, JSON.stringify(cache || {}, null, 2), 'utf8');
    return true;
  } catch {
    return false;
  }
}

/** 域名归一：小写、去端口、去前导点。 */
export function normalizeHost(host) {
  return String(host || '')
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, '')
    .replace(/\/.*$/, '')
    .replace(/:\d+$/, '')
    .replace(/^\.+/, '');
}

function fresh(entry) {
  if (!entry) return false;
  if (!entry.updatedAt) return true;
  return Date.now() - new Date(entry.updatedAt).getTime() < MAX_AGE_MS;
}

/** 精确/子域匹配：session.example.com 命中 example.com 的条目。 */
export function sessionFor(host) {
  const h = normalizeHost(host);
  if (!h) return null;
  const all = load();
  const hit = all[h];
  if (hit && fresh(hit)) return { host: h, ...hit };
  // 父域回退（a.b.com → b.com → com 最多两级）
  const parts = h.split('.');
  for (let i = 1; i <= 2 && i < parts.length - 1; i++) {
    const parent = parts.slice(i).join('.');
    const p = all[parent];
    if (p && fresh(p)) return { host: parent, ...p, matchedFrom: h };
  }
  return null;
}

/** 给 fetch 用的头：Cookie / Referer / 额外头。 */
export function sessionHeaders(url) {
  try {
    const u = new URL(url);
    const s = sessionFor(u.hostname);
    if (!s) return null;
    const headers = { ...(s.headers || {}) };
    if (s.cookie) headers.Cookie = s.cookie;
    if (s.referer) headers.Referer = s.referer;
    if (s.userAgent) headers['User-Agent'] = s.userAgent;
    return headers;
  } catch {
    return null;
  }
}

export function listSessions() {
  const all = load();
  return Object.entries(all).map(([host, v]) => ({
    host,
    updatedAt: v.updatedAt || null,
    hasCookie: !!v.cookie,
    cookieLen: v.cookie ? v.cookie.length : 0,
    storageKeys: Array.isArray(v.storage) ? v.storage.map((x) => x.key) : [],
    headerKeys: v.headers ? Object.keys(v.headers) : [],
    note: v.note || '',
    stale: !fresh(v),
    source: v.source || 'manual',
  }));
}

/**
 * 写入/更新一条会话。
 * @param {string} host 域名（可带路径，会被归一）
 * @param {{cookie?:string, storage?:Array<{key:string,value:string}>, headers?:object, note?:string, source?:string, userAgent?:string, referer?:string}} data
 */
export function saveSession(host, data = {}) {
  const h = normalizeHost(host);
  if (!h || !/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(h)) {
    const err = new Error('host 不合法');
    err.status = 400;
    throw err;
  }
  const all = load();
  const prev = all[h] || {};
  const storage = Array.isArray(data.storage)
    ? data.storage
      .filter((x) => x && typeof x.key === 'string')
      .slice(0, 200)
      .map((x) => ({ key: String(x.key), value: String(x.value == null ? '' : x.value).slice(0, 8000) }))
    : prev.storage || [];
  const cookie = String(data.cookie ?? prev.cookie ?? '').slice(0, 12000);
  const headers = {};
  for (const [k, v] of Object.entries(data.headers || prev.headers || {})) {
    const key = String(k).replace(/[^\w-]/g, '');
    if (!key || /^(cookie|host|content-length)$/i.test(key)) continue;
    if (v == null) continue;
    headers[key] = String(v).slice(0, 4000);
  }
  all[h] = {
    ...prev,
    cookie,
    storage,
    headers,
    note: String(data.note ?? prev.note ?? '').slice(0, 200),
    source: String(data.source ?? prev.source ?? 'manual').slice(0, 40),
    userAgent: data.userAgent ? String(data.userAgent).slice(0, 300) : prev.userAgent,
    referer: data.referer ? String(data.referer).slice(0, 500) : prev.referer,
    updatedAt: new Date().toISOString(),
  };
  persist();
  return { host: h, ...all[h] };
}

/** localStorage 里某个 key 的值（海角 uuid、hstv 的 uu1d 等从这里取）。 */
export function sessionStorageValue(host, key) {
  const s = sessionFor(host);
  if (!s || !Array.isArray(s.storage)) return null;
  const hit = s.storage.find((x) => x.key === key);
  return hit ? hit.value : null;
}

export function deleteSession(host) {
  const h = normalizeHost(host);
  const all = load();
  if (!h) {
    cache = {};
    persist();
    return { cleared: true };
  }
  const existed = !!all[h];
  delete all[h];
  persist();
  return { host: h, removed: existed };
}

export function sessionFilePath() {
  return FILE;
}

/** 只允许本机访问会话写接口（可用 SWIPEFEED_ALLOW_REMOTE_SESSION=1 放开）。 */
export function isLocalRequest(req) {
  if (String(process.env.SWIPEFEED_ALLOW_REMOTE_SESSION || '') === '1') return true;
  const raw = req.socket?.remoteAddress || req.ip || '';
  const addr = raw.replace(/^::ffff:/, '');
  return addr === '127.0.0.1' || addr === '::1' || addr === 'localhost';
}
