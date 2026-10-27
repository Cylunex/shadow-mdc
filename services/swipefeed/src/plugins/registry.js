import { demo } from './demo.js';
import { pornhub } from './pornhub.js';
import { xvideos } from './xvideos.js';
import {
  xhamster, xnxx, spankbang, eporner, youporn, youjizz,
} from './core_rest.js';
import { beegApi as beeg } from './beeg_api.js';
import {
  redtube, tube8, txxx, drtuber, tnaflix, empflix, porntube, sunporno,
  thisvid, hellporno, fourtube, porntop, peekvids, redgifs, hqporner,
  porntrex, threemovs, pornone, anysex, pornhat, okxxx, porngo,
} from './extras.js';
import {
  zbporn, rule34video, perfectgirls, anyporn, superporn, sexvid, bigfuck, porndoe,
} from './extras_batch2.js';
import { AGGREGATOR_SOURCES } from './aggregators.js';
import { pektino } from './pektino.js';
import { threego } from './threego.js';
import { haijiao } from './haijiao.js';
import { videoshub } from './videoshub.js';
import { mrds } from './mrds.js';
import { hanxiucao } from './hanxiucao.js';
import { CATEGORIES, CATEGORY_TEMPLATES, SITE_BASE } from './categories.js';
import { isSiteEnabled, loadSiteSettings } from '../util/site-settings.js';
import {
  recordOk, recordFail, isCircuitAllowing, weightedSiteOrder,
} from '../util/circuit-breaker.js';

/** Primary 9 + aggregators + extras + batch2 + demo. */
export const ALL_SOURCES = [
  pornhub, xvideos, xhamster, xnxx, spankbang, eporner, youporn, beeg, youjizz,
  ...AGGREGATOR_SOURCES,
  pektino, threego, haijiao, videoshub, mrds, hanxiucao,
  redtube, tube8, txxx, drtuber, tnaflix, empflix, porntube, sunporno,
  thisvid, hellporno, fourtube, porntop, peekvids, redgifs, hqporner,
  porntrex, threemovs, pornone, anysex, pornhat, okxxx, porngo,
  zbporn, rule34video, perfectgirls, anyporn, superporn, sexvid, bigfuck, porndoe,
  demo,
];

const byId = new Map(ALL_SOURCES.map((s) => [s.id, s]));

// Ensure defaults file exists early
try { loadSiteSettings(); } catch { /* */ }

export function getSource(id) {
  return byId.get(id) || null;
}

/**
 * 分类模板：站点自家形状优先（插件暴露的 categoryPattern / categoryTemplate），
 * 其次 CATEGORY_TEMPLATES 里人工登记的（手工实现的插件用）。
 * 动态探测与分类过滤都读这一个来源，保证“探测出来的 slug 一定能出列表”。
 */
export function categoryTemplateOf(id) {
  const src = byId.get(id);
  if (src) {
    if (typeof src.categoryPattern === 'string' && src.categoryPattern) return src.categoryPattern;
    if (typeof src.categoryTemplate === 'string' && src.categoryTemplate) return src.categoryTemplate;
  }
  return CATEGORY_TEMPLATES[id] || null;
}

/** 只拼分类 URL，取数交给各源自己的解析。 */
export function categoryUrlFor(id, slug, page = 1) {
  const tpl = categoryTemplateOf(id);
  if (!tpl || !slug) return null;
  const src = byId.get(id);
  const base = (src && typeof src.base === 'string' && src.base) || SITE_BASE[id] || null;
  if (!base) return null;
  const path = tpl.replace('{slug}', encodeURIComponent(String(slug)));
  try {
    const url = new URL(path.startsWith('/') ? path : `/${path}`, base);
    if (page > 1 && !/page=/.test(url.search)) url.searchParams.set('page', String(page));
    return url.toString();
  } catch {
    return null;
  }
}

/** All registered sites (including disabled) — for admin. */
export function listAllSites() {
  return ALL_SOURCES.map((s) => ({
    id: s.id,
    name: s.name || s.displayName || s.id,
    displayName: s.displayName || s.name || s.id,
    enabled: isSiteEnabled(s.id),
  }));
}

/** Default feed / UI list — disabled sites excluded. */
export function listSites({ includeDisabled = false } = {}) {
  return ALL_SOURCES
    .filter((s) => includeDisabled || isSiteEnabled(s.id))
    .map((s) => ({
      id: s.id,
      name: s.name || s.displayName || s.id,
      displayName: s.displayName || s.name || s.id,
      enabled: isSiteEnabled(s.id),
    }));
}

export { CATEGORIES };

export function listToolBackedSources() {
  return ALL_SOURCES
    .filter((s) => TOOL_BACKED_IDS.includes(s.id))
    .map((s) => ({
      id: s.id,
      name: s.name || s.displayName || s.id,
      displayName: s.displayName || s.name || s.id,
      enabled: isSiteEnabled(s.id),
    }));
}

const TOOL_BACKED_IDS = ['hsex', 'pektino', 'threego', 'haijiao', 'videoshub', 'rou', 'x134', 'bilifun', 'netflav'];

/** Mix pool: primary tubes + new aggregators (weight 1 each). */
const MIXABLE = [
  'pornhub', 'xvideos', 'xhamster', 'xnxx', 'spankbang',
  'eporner', 'youporn', 'beeg', 'youjizz',
  'rou', 'x134', 'bilifun', 'netflav', 'hsex', 'aggrcine', 'pektino', 'threego', 'haijiao', 'videoshub', 'mrds',
];

/**
 * 随机混排：**按源轮流发牌**，不是把某个源的一页整段塞进来。
 *
 * 之前的实现是「按权重排源 → 逐个源取一页 → 连续 push 到满 24 条」，
 * 结果第一批 24 条里前面全是同一个源，看起来就是“刷半天一个源”。
 *
 * 现在每个源维护自己的待发池 + 已发集合：
 *   · 池子空了就单独补货（页号从随机起点开始递增，避免永远只要第一页）
 *   · 出卡时每轮从不同源抽一张，相邻两张不会是同一源（只要还有别的源可用）
 *   · 池子越大越容易被抽到，但健康度差的源自然出得少（熔断仍会跳过）
 */
const MIX_REFILL_MIN = 6;      // 池里少于这么多就补货
const MIX_REFILL_SITES = 6;    // 每次最多同时补几个源
const MIX_BATCH = 24;          // 一次返回多少张卡
const MIX_POOL_MAX = 12;       // 单源池子上限（小池子才轮换得动）
const MIX_SEEN_MAX = 2000;     // 单源去重记忆上限

const mixPools = new Map();    // id -> { items, seen, page, fetching, lastFill }
const mixOrder = { last: null };

function mixPool(id) {
  let st = mixPools.get(id);
  if (!st) {
    st = { items: [], seen: new Set(), page: 0, fetching: null, lastFill: 0, used: 0 };
    mixPools.set(id, st);
  }
  return st;
}

function cardKey(card) {
  return `${card.site}:${card.id}`;
}

function shuffled(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

/** 补货：只拉一页，页号从随机起点递增；失败就重置页号，下次换起点。 */
async function refillMixSite(id, category) {
  const st = mixPool(id);
  if (st.fetching) return st.fetching;
  const src = byId.get(id);
  if (!src) return null;
  st.fetching = (async () => {
    if (!st.page) st.page = 1 + Math.floor(Math.random() * 3);
    const usePage = st.page;
    st.page += 1;
    const t0 = Date.now();
    try {
      const result = category
        ? await src.categoryPage(category, { page: usePage })
        : await src.nextPage({ page: id === 'xvideos' || id === 'beeg' ? Math.max(0, usePage - 1) : usePage });
      const items = result?.items || [];
      const ms = Date.now() - t0;
      if (items.length) recordOk(id, ms);
      else recordFail(id, 'empty page', ms);

      const fresh = [];
      for (const card of shuffled(items.slice())) {
        const key = cardKey(card);
        if (st.seen.has(key)) continue;
        st.seen.add(key);
        fresh.push(card);
      }
      if (st.seen.size > MIX_SEEN_MAX) st.seen.clear();
      st.items.push(...fresh);
      if (st.items.length > MIX_POOL_MAX) st.items.length = MIX_POOL_MAX;
      st.lastFill = Date.now();
      if (!items.length) {
        // 翻到空页：回到随机起点
        st.page = 0;
      } else if (!fresh.length) {
        // 整页都是发过的 → 这个源的视频已经轮完一遍，清掉记忆重新开始，
        // 否则会一直“翻页全是旧卡”，池子永远补不进来
        st.seen.clear();
        st.page = 0;
      }
    } catch (e) {
      recordFail(id, e.message || String(e), Date.now() - t0);
      st.page = 0;
    } finally {
      st.fetching = null;
    }
    return st;
  })();
  return st.fetching;
}

/** 发牌：每轮挑一个源（源不同、池大优先），取一张。 */
function drawMixedBatch(ids, want) {
  const out = [];
  let last = mixOrder.last;
  while (out.length < want) {
    const candidates = ids.filter((id) => (mixPools.get(id)?.items.length || 0) > 0);
    if (!candidates.length) break;
    const usable = candidates.length > 1 ? candidates.filter((id) => id !== last) : candidates;
    // Efraimidis–Spirakis：random^(1/len) 取最大 → 池子大的更容易被选中
    let pick = usable[0];
    let best = -Infinity;
    for (const id of usable) {
      const st = mixPools.get(id);
      // 池子大优先，但已经出得多的源降权，让别的源轮进来
      const w = Math.max(1, st.items.length / (1 + st.used / 8));
      const key = Math.random() ** (1 / w);
      if (key > best) {
        best = key;
        pick = id;
      }
    }
    const card = mixPools.get(pick).items.shift();
    mixPools.get(pick).used += 1;
    if (!card) continue;
    out.push(card);
    last = pick;
  }
  mixOrder.last = last;
  return out;
}

export async function mixedNextPage(cursor, { category = null, sites = null } = {}) {
  const pool = (sites && sites.length ? sites : MIXABLE)
    .filter((id) => id !== 'demo' && byId.has(id) && isSiteEnabled(id));
  const page = cursor?.page ?? 0;
  const explicitSites = Array.isArray(sites) && sites.length > 0;

  // 可用源：熔断允许的优先；全都开着熔断才退回全量，保证不空
  const allowed = pool.filter((id) => isCircuitAllowing(id));
  const ids = allowed.length ? allowed : pool;
  if (!ids.length) return { items: [], nextCursor: null, hasMore: false };

  const pooled = () => ids.reduce((n, id) => n + (mixPools.get(id)?.items.length || 0), 0);
  const withItems = () => ids.filter((id) => (mixPools.get(id)?.items.length || 0) > 0).length;
  const refillTargets = () => {
    // 池子空/薄的源里，优先补“还没怎么出过”的，避免总是同几个源轮转
    const order = weightedSiteOrder(ids, { includeOpen: false });
    const base = order.length ? order : ids;
    return base
      .filter((id) => (mixPools.get(id)?.items.length || 0) < MIX_REFILL_MIN && !mixPools.get(id)?.fetching)
      .map((id, i) => ({ id, used: mixPools.get(id)?.used || 0, jitter: Math.random(), i }))
      .sort((a, b) => (a.used - b.used) || (a.jitter - b.jitter) || (a.i - b.i))
      .slice(0, explicitSites ? MIX_REFILL_SITES * 4 : MIX_REFILL_SITES)
      .map((x) => x.id);
  };
  /** 至少要几个源有货，否则发牌会退化成一两个源来回抽 */
  const minSources = Math.min(8, ids.length);

  /** 补货一轮：新开的 + 已经在跑的请求一起等（否则 warmAll 把目标抢走后这里会直接返回 0 条）。 */
  const refillRound = async () => {
    const pending = [];
    for (const id of ids) {
      if (pending.length >= 16) break;
      const st = mixPools.get(id);
      if (st?.fetching) {
        pending.push(st.fetching);
      } else if ((st?.items.length || 0) < MIX_REFILL_MIN) {
        pending.push(refillMixSite(id, category).catch(() => null));
      }
    }
    if (!pending.length) return;
    await Promise.race([
      Promise.allSettled(pending),
      new Promise((r) => setTimeout(r, 9000)),
    ]);
  };

  /** 全部源一起点火（不等结果），让后续几页不用再等。 */
  const warmAll = () => {
    for (const id of ids) {
      const st = mixPools.get(id);
      if ((!st || st.items.length < MIX_REFILL_MIN) && !st?.fetching) {
        refillMixSite(id, category).catch(() => {});
      }
    }
  };

  if (withItems() < minSources) warmAll();

  // 总库存见底，或有货的源太少：等补货（有上限，慢源不会拖死整页）
  if (pooled() < MIX_BATCH || withItems() < minSources) {
    await refillRound();
  }

  let batch = drawMixedBatch(ids, MIX_BATCH);

  // 发的不够：再来两轮补货（每轮换一批源）
  for (let round = 0; round < 2 && batch.length < MIX_BATCH; round++) {
    await refillRound();
    const more = drawMixedBatch(ids, MIX_BATCH - batch.length);
    const have = new Set(batch.map(cardKey));
    batch = batch.concat(more.filter((c) => !have.has(cardKey(c))));
  }

  // 后台补货：让下一屏不必再等
  for (const id of refillTargets().slice(0, 3)) {
    refillMixSite(id, category).catch(() => {});
  }

  if (batch.length) mixOrder.last = batch[batch.length - 1].site;
  return {
    items: batch,
    nextCursor: batch.length ? { page: page + 1 } : null,
    hasMore: batch.length > 0,
  };
}

/** 聚合搜索参数：慢源排除、每源上限、总量、并发。 */
const SEARCH_SLOW = new Set(['txxx', 'hqporner', 'beeg']);
const SEARCH_PER_SOURCE = 12;
const SEARCH_TOTAL = 30;
const SEARCH_CONCURRENCY = 6;
const SEARCH_MAX_SOURCES = 12;
const SEARCH_DEADLINE_MS = 9000;   // 聚合搜索总预算，超时就返回已有的
const SEARCH_PER_SOURCE_TIMEOUT_MS = 7000;

export async function searchAll(q, cursor) {
  const page = cursor?.page ?? 0;
  // 跑无头浏览器的源不参与聚合搜索（单站搜索仍然可用），否则一次搜索要等几十秒
  const pool = ALL_SOURCES
    .filter((s) => s.id !== 'demo' && isSiteEnabled(s.id) && !SEARCH_SLOW.has(s.id))
    .map((s) => s.id);
  const orderIds = weightedSiteOrder(pool, { includeOpen: false });
  const ids = (orderIds.length ? orderIds : pool)
    .filter((id) => byId.has(id) && (orderIds.length ? isCircuitAllowing(id) : true))
    .slice(0, SEARCH_MAX_SOURCES);

  const buckets = [];
  const runOne = async (id) => {
    const src = byId.get(id);
    if (!src) return null;
    const t0 = Date.now();
    try {
      const result = await Promise.race([
        src.search(q, { page: page + 1 }),
        new Promise((resolve) => setTimeout(() => resolve(null), SEARCH_PER_SOURCE_TIMEOUT_MS)),
      ]);
      if (!result) return null;
      const items = (result.items || []).slice(0, SEARCH_PER_SOURCE);
      const ms = Date.now() - t0;
      if (items.length) recordOk(id, ms);
      else recordFail(id, 'empty search', ms);
      return items;
    } catch (e) {
      recordFail(id, e.message || String(e), Date.now() - t0);
      return null;
    }
  };
  const deadline = Date.now() + SEARCH_DEADLINE_MS;
  for (let i = 0; i < ids.length; i += SEARCH_CONCURRENCY) {
    if (Date.now() > deadline && buckets.length) break;
    const chunk = ids.slice(i, i + SEARCH_CONCURRENCY);
    // eslint-disable-next-line no-await-in-loop
    const got = await Promise.all(chunk.map((id) => runOne(id)));
    got.forEach((items) => { if (items && items.length) buckets.push(items); });
    if (buckets.reduce((n, b) => n + b.length, 0) >= SEARCH_TOTAL) break;
  }

  // 轮流发牌：避免第一个源把 30 条全占满（以前「搜索只出一个源」就是这原因）
  const batch = [];
  const seen = new Set();
  let round = 0;
  while (batch.length < SEARCH_TOTAL) {
    let added = 0;
    for (const bucket of buckets) {
      const card = bucket[round];
      if (!card) continue;
      const key = `${card.site}:${card.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      batch.push(card);
      added += 1;
      if (batch.length >= SEARCH_TOTAL) break;
    }
    if (!added) break;
    round += 1;
  }

  return {
    items: batch,
    nextCursor: batch.length ? { page: page + 1 } : null,
    hasMore: batch.length > 0,
  };
}
