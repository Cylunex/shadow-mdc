/**
 * In-memory per-site circuit breaker + latency weighting for mix/search/resolve.
 * Does not override manual enable/disable; exposes auto-break for admin UI.
 */

const FAIL_OPEN_THRESHOLD = 3;
const COOL_DOWN_MS = 120_000;
const LATENCY_WINDOW = 20;
const HALF_OPEN_PROBE_WEIGHT = 0.15;

/** @typedef {'closed'|'open'|'half-open'} CircuitState */

/**
 * @typedef {object} SiteHealth
 * @property {string} site
 * @property {number} success
 * @property {number} fail
 * @property {number} consecutiveFails
 * @property {number[]} latencies
 * @property {CircuitState} state
 * @property {number|null} openedAt
 * @property {number} coolDownMs
 * @property {string} lastError
 * @property {number|null} lastOkAt
 * @property {number|null} lastFailAt
 * @property {number|null} avgLatencyMs
 * @property {number} healthScore
 * @property {boolean} autoBroken
 */

/** @type {Map<string, Omit<SiteHealth,'avgLatencyMs'|'healthScore'|'autoBroken'|'site'>>} */
const bySite = new Map();

function blank() {
  return {
    success: 0,
    fail: 0,
    consecutiveFails: 0,
    latencies: [],
    state: /** @type {CircuitState} */ ('closed'),
    openedAt: null,
    coolDownMs: COOL_DOWN_MS,
    lastError: '',
    lastOkAt: null,
    lastFailAt: null,
  };
}

function ensure(site) {
  let h = bySite.get(site);
  if (!h) {
    h = blank();
    bySite.set(site, h);
  }
  return h;
}

function avgLatency(latencies) {
  if (!latencies.length) return null;
  return Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length);
}

/**
 * 0..1 score — higher is healthier. Open circuits score 0 until half-open.
 */
export function healthScore(site) {
  const h = ensure(site);
  reconcileState(h);
  if (h.state === 'open') return 0;
  if (h.state === 'half-open') return HALF_OPEN_PROBE_WEIGHT;

  const total = h.success + h.fail;
  const successRate = total === 0 ? 0.7 : h.success / total; // unknown → mild prior
  const avg = avgLatency(h.latencies);
  // Map latency: 200ms→1, 2000ms→0.2, 5000ms+→0.05
  let latencyFactor = 0.85;
  if (avg != null) {
    latencyFactor = Math.max(0.05, Math.min(1, 800 / Math.max(200, avg)));
  }
  const consecPenalty = Math.max(0, 1 - h.consecutiveFails * 0.25);
  return Math.max(0.01, successRate * 0.55 + latencyFactor * 0.35 + consecPenalty * 0.1);
}

function reconcileState(h) {
  if (h.state === 'open' && h.openedAt != null) {
    if (Date.now() - h.openedAt >= h.coolDownMs) {
      h.state = 'half-open';
    }
  }
}

/** Whether list/resolve traffic may hit this site now. */
export function isCircuitAllowing(site) {
  const h = ensure(site);
  reconcileState(h);
  return h.state !== 'open';
}

export function recordOk(site, latencyMs = 0) {
  if (!site) return;
  const h = ensure(site);
  h.success += 1;
  h.consecutiveFails = 0;
  h.lastOkAt = Date.now();
  h.lastError = '';
  if (latencyMs > 0) {
    h.latencies.push(Math.round(latencyMs));
    if (h.latencies.length > LATENCY_WINDOW) h.latencies.shift();
  }
  h.state = 'closed';
  h.openedAt = null;
}

export function recordFail(site, errMsg = '', latencyMs = 0) {
  if (!site) return;
  const h = ensure(site);
  h.fail += 1;
  h.consecutiveFails += 1;
  h.lastFailAt = Date.now();
  h.lastError = String(errMsg || '').slice(0, 200);
  if (latencyMs > 0) {
    h.latencies.push(Math.round(latencyMs));
    if (h.latencies.length > LATENCY_WINDOW) h.latencies.shift();
  }
  if (h.state === 'half-open' || h.consecutiveFails >= FAIL_OPEN_THRESHOLD) {
    h.state = 'open';
    h.openedAt = Date.now();
  }
}

export function resetCircuit(site) {
  if (!site) return null;
  const h = blank();
  bySite.set(site, h);
  return snapshot(site);
}

export function resetAllCircuits() {
  bySite.clear();
  return { ok: true, cleared: true };
}

function snapshot(site) {
  const h = ensure(site);
  reconcileState(h);
  const avg = avgLatency(h.latencies);
  const score = healthScore(site);
  return {
    site,
    success: h.success,
    fail: h.fail,
    consecutiveFails: h.consecutiveFails,
    latencies: h.latencies.slice(),
    state: h.state,
    openedAt: h.openedAt,
    coolDownMs: h.coolDownMs,
    coolDownRemainingMs:
      h.state === 'open' && h.openedAt
        ? Math.max(0, h.coolDownMs - (Date.now() - h.openedAt))
        : 0,
    lastError: h.lastError,
    lastOkAt: h.lastOkAt,
    lastFailAt: h.lastFailAt,
    avgLatencyMs: avg,
    healthScore: Math.round(score * 1000) / 1000,
    autoBroken: h.state === 'open',
  };
}

export function getSiteHealth(site) {
  return snapshot(site);
}

export function getAllHealth() {
  const out = [];
  for (const site of bySite.keys()) out.push(snapshot(site));
  out.sort((a, b) => a.site.localeCompare(b.site));
  return out;
}

/**
 * Color hint for UI: green / yellow / red / gray (unknown).
 * @returns {'green'|'yellow'|'red'|'gray'}
 */
export function healthColor(site) {
  const h = ensure(site);
  reconcileState(h);
  if (h.success === 0 && h.fail === 0) return 'gray';
  if (h.state === 'open') return 'red';
  if (h.state === 'half-open' || h.consecutiveFails > 0) return 'yellow';
  const score = healthScore(site);
  if (score >= 0.55) return 'green';
  if (score >= 0.3) return 'yellow';
  return 'red';
}

/**
 * Weighted random order — healthier sites tend to appear earlier.
 * Open-circuit sites are excluded unless includeOpen.
 */
export function weightedSiteOrder(siteIds, { includeOpen = false } = {}) {
  const scored = [];
  for (const id of siteIds) {
    if (!includeOpen && !isCircuitAllowing(id)) continue;
    const w = Math.max(0.01, healthScore(id));
    scored.push({ id, w, key: Math.random() ** (1 / w) });
  }
  // Efraimidis–Spirakis weighted shuffle key
  scored.sort((a, b) => b.key - a.key);
  return scored.map((s) => s.id);
}

export const CIRCUIT_CONSTANTS = {
  FAIL_OPEN_THRESHOLD,
  COOL_DOWN_MS,
  LATENCY_WINDOW,
};
