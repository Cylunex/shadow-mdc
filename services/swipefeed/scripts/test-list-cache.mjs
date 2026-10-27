#!/usr/bin/env node
/**
 * 列表缓存（TTL + 单飞 + 上限淘汰）离线测试。
 * 跑法：node scripts/test-list-cache.mjs
 */

import assert from 'node:assert/strict';
import { cached, invalidate, cacheStats, cacheDisabled } from '../src/util/list-cache.js';

let pass = 0;
async function check(name, fn) {
  try {
    await fn();
    pass += 1;
    console.log(`  ✓ ${name}`);
  } catch (e) {
    console.error(`  ✗ ${name}\n    ${e.message}`);
    process.exitCode = 1;
  }
}

process.env.SWIPEFEED_CACHE_TTL_MS = '40';

let calls = 0;
const producer = async () => {
  calls += 1;
  await new Promise((r) => setTimeout(r, 15));
  return `page-${calls}`;
};

console.log('list-cache');
await check('首次未命中，打一次上游', async () => {
  invalidate();
  calls = 0;
  const v = await cached('k1', producer);
  assert.equal(v, 'page-1');
  assert.equal(calls, 1);
});

await check('TTL 内命中缓存，不再打上游', async () => {
  const v = await cached('k1', producer);
  assert.equal(v, 'page-1');
  assert.equal(calls, 1);
});

await check('并发同页只打一次上游（单飞）', async () => {
  invalidate();
  calls = 0;
  const [a, b, c] = await Promise.all([
    cached('k2', producer), cached('k2', producer), cached('k2', producer),
  ]);
  assert.equal(calls, 1);
  assert.equal(a, b);
  assert.equal(b, c);
});

await check('TTL 过期后重新拉取', async () => {
  await new Promise((r) => setTimeout(r, 60));
  const v = await cached('k2', producer);
  assert.equal(calls, 2);
  assert.equal(v, 'page-2');
});

await check('失败不缓存：异常原样抛出，下次仍会重试', async () => {
  invalidate();
  let n = 0;
  const bad = async () => {
    n += 1;
    if (n === 1) throw new Error('boom');
    return 'ok';
  };
  await assert.rejects(() => cached('k3', bad), /boom/);
  assert.equal(await cached('k3', bad), 'ok');
  assert.equal(n, 2);
});

await check('条目上限：超过 200 条淘汰最旧', async () => {
  invalidate();
  process.env.SWIPEFEED_CACHE_TTL_MS = '5000';
  for (let i = 0; i < 215; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await cached(`bulk-${i}`, async () => i);
  }
  const stats = cacheStats();
  assert.ok(stats.size <= 200, `size=${stats.size}`);
  assert.ok(stats.evicted >= 15, `evicted=${stats.evicted}`);
});

await check('SWIPEFEED_NO_CACHE=1 直接穿透', async () => {
  invalidate();
  process.env.SWIPEFEED_NO_CACHE = '1';
  let n = 0;
  const p = async () => { n += 1; return n; };
  assert.equal(cacheDisabled(), true);
  await cached('k9', p);
  await cached('k9', p);
  assert.equal(n, 2);
  delete process.env.SWIPEFEED_NO_CACHE;
});

console.log(`\n${pass} 项断言组通过${process.exitCode ? '（存在失败）' : ''}`);
