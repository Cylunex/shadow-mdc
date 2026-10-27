/**
 * 列表页短 TTL 缓存 + 单飞（single-flight）。
 *
 * 目的：滚动/切站/回退时同一页会被反复请求，既拖慢体感又容易被站点限速。
 * 这里给 `factory.listFrom` 加一层进程内缓存：
 *   · TTL 内重复请求直接命中内存；
 *   · 同一 URL 的并发请求只打一次上游（`inflight`），其余等同一个 promise；
 *   · 失败不缓存，异常原样抛给调用方；
 *   · 条目数有上限，按插入顺序淘汰，不会无限涨内存。
 *
 * 关掉缓存：`SWIPEFEED_NO_CACHE=1`（探测脚本 / 排障时用）。
 */

const MAX_ENTRIES = 200;
const DEFAULT_TTL_MS = 60_000;

const store = new Map();      // key -> { exp, value }
const inflight = new Map();   // key -> Promise
const counters = { hit: 0, miss: 0, coalesced: 0, evicted: 0 };

export function cacheDisabled() {
  return process.env.SWIPEFEED_NO_CACHE === '1';
}

export function ttlMs(value) {
  const fromEnv = Number(process.env.SWIPEFEED_CACHE_TTL_MS);
  if (Number.isFinite(fromEnv) && fromEnv >= 0) return fromEnv;
  return value ?? DEFAULT_TTL_MS;
}

/**
 * @template T
 * @param {string} key 缓存键（一般直接用列表 URL）
 * @param {() => Promise<T>} producer 真正打上游的函数
 * @param {number} [ttl] 毫秒，默认 60s
 * @returns {Promise<T>}
 */
export async function cached(key, producer, ttl) {
  if (cacheDisabled()) return producer();

  const now = Date.now();
  const hit = store.get(key);
  if (hit) {
    if (hit.exp > now) {
      counters.hit += 1;
      store.delete(key); // 刷新 LRU 顺序
      store.set(key, hit);
      return hit.value;
    }
    store.delete(key);
  }

  const pending = inflight.get(key);
  if (pending) {
    counters.coalesced += 1;
    return pending;
  }

  counters.miss += 1;
  const task = (async () => {
    try {
      const value = await producer();
      const life = ttlMs(ttl);
      if (life > 0) {
        store.set(key, { exp: Date.now() + life, value });
        while (store.size > MAX_ENTRIES) {
          const oldest = store.keys().next().value;
          store.delete(oldest);
          counters.evicted += 1;
        }
      }
      return value;
    } finally {
      inflight.delete(key);
    }
  })();

  inflight.set(key, task);
  return task;
}

export function invalidate(prefix) {
  if (!prefix) {
    store.clear();
    return;
  }
  for (const key of [...store.keys()]) {
    if (key.startsWith(prefix)) store.delete(key);
  }
}

export function cacheStats() {
  return { size: store.size, inflight: inflight.size, ...counters };
}
