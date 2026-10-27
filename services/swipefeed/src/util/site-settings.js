/**
 * Persisted enable/disable map for feed sources.
 * File: data/site-settings.json (created on first write / boot merge).
 * Sites stay in code; disabled ones are filtered from UI / mix / search.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = process.env.SWIPEFEED_DATA_DIR
  ? path.resolve(process.env.SWIPEFEED_DATA_DIR)
  : path.join(__dirname, '..', '..', 'data');
const SETTINGS_PATH = path.join(DATA_DIR, 'site-settings.json');

/**
 * Default-off for known dead / CF-hard sites (even if not currently registered).
 * Notes surface in admin UI; user can re-enable without redeploy.
 */
export const DEFAULT_DISABLED = {
  motherless: 'dead / unreachable (fetch failed)',
  bitporno: 'dead / unreachable (fetch failed)',
  noodlemagazine: 'Cloudflare 403 after headers+proxy+retry',
  '4tube': 'Cloudflare 403 after headers+proxy+retry',
  spankbang: 'Cloudflare 403 after headers+proxy+retry',
  pornone: 'Cloudflare 403 after headers+proxy+retry',
};

/** @typedef {{ enabled: boolean, note?: string }} SiteFlag */
/** @typedef {{ updatedAt: string|null, sites: Record<string, SiteFlag> }} SiteSettings */

/** @type {SiteSettings|null} */
let cache = null;

function emptySettings() {
  return { updatedAt: null, sites: {} };
}

function ensureDefaults(settings) {
  for (const [id, note] of Object.entries(DEFAULT_DISABLED)) {
    if (!settings.sites[id]) {
      settings.sites[id] = { enabled: false, note };
    }
  }
  return settings;
}

export function getSettingsPath() {
  return SETTINGS_PATH;
}

export function loadSiteSettings() {
  if (cache) return cache;
  let parsed = emptySettings();
  try {
    if (fs.existsSync(SETTINGS_PATH)) {
      const raw = fs.readFileSync(SETTINGS_PATH, 'utf8');
      const j = JSON.parse(raw);
      parsed = {
        updatedAt: j.updatedAt || null,
        sites: j.sites && typeof j.sites === 'object' ? j.sites : {},
      };
    }
  } catch (e) {
    console.warn('[site-settings] load failed:', e.message);
    parsed = emptySettings();
  }
  cache = ensureDefaults(parsed);
  // Persist defaults on first boot so admin sees them without a write
  try {
    if (!fs.existsSync(SETTINGS_PATH)) {
      saveSiteSettings(cache);
    }
  } catch (e) {
    console.warn('[site-settings] bootstrap write failed:', e.message);
  }
  return cache;
}

export function saveSiteSettings(settings) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const out = {
    updatedAt: new Date().toISOString(),
    sites: settings.sites || {},
  };
  const tmp = SETTINGS_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(out, null, 2) + '\n', 'utf8');
  fs.renameSync(tmp, SETTINGS_PATH);
  cache = out;
  return cache;
}

export function reloadSiteSettings() {
  cache = null;
  return loadSiteSettings();
}

/** @param {string} id */
export function isSiteEnabled(id) {
  const s = loadSiteSettings();
  const flag = s.sites[id];
  if (!flag) return true; // unknown → enabled by default
  return flag.enabled !== false;
}

/**
 * Merge registry sites with settings for admin / API.
 * @param {Array<{id:string,name?:string,displayName?:string}>} registrySites
 */
export function listSiteSettings(registrySites) {
  const s = loadSiteSettings();
  const seen = new Set();
  const rows = [];
  for (const site of registrySites) {
    seen.add(site.id);
    const flag = s.sites[site.id];
    const enabled = flag ? flag.enabled !== false : true;
    rows.push({
      id: site.id,
      name: site.displayName || site.name || site.id,
      displayName: site.displayName || site.name || site.id,
      enabled,
      note: flag?.note || (DEFAULT_DISABLED[site.id] || ''),
      inRegistry: true,
    });
  }
  // orphans in DEFAULT_DISABLED / settings not in registry
  for (const [id, flag] of Object.entries(s.sites)) {
    if (seen.has(id)) continue;
    rows.push({
      id,
      name: id,
      displayName: id,
      enabled: flag.enabled !== false,
      note: flag.note || DEFAULT_DISABLED[id] || '',
      inRegistry: false,
    });
  }
  rows.sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
  return { updatedAt: s.updatedAt, sites: rows };
}

/**
 * Apply partial updates: { sites: { id: { enabled, note? } } } or array of {id,enabled,note?}
 * @param {object} body
 */
export function updateSiteSettings(body) {
  const s = loadSiteSettings();
  const updates = body?.sites;
  if (!updates) throw Object.assign(new Error('sites required'), { status: 400 });

  const applyOne = (id, patch) => {
    if (!id || typeof id !== 'string') return;
    const prev = s.sites[id] || { enabled: true, note: DEFAULT_DISABLED[id] || '' };
    const next = {
      enabled: patch.enabled !== undefined ? !!patch.enabled : prev.enabled !== false,
      note: patch.note !== undefined ? String(patch.note || '') : (prev.note || ''),
    };
    // Keep default note if clearing to empty and we have a known reason
    if (!next.note && DEFAULT_DISABLED[id]) next.note = DEFAULT_DISABLED[id];
    s.sites[id] = next;
  };

  if (Array.isArray(updates)) {
    for (const row of updates) applyOne(row.id, row);
  } else if (typeof updates === 'object') {
    for (const [id, patch] of Object.entries(updates)) {
      applyOne(id, patch && typeof patch === 'object' ? patch : { enabled: !!patch });
    }
  } else {
    throw Object.assign(new Error('sites must be object or array'), { status: 400 });
  }

  return saveSiteSettings(s);
}

/** Filter id list to enabled only. */
export function filterEnabledIds(ids) {
  return ids.filter((id) => isSiteEnabled(id));
}
