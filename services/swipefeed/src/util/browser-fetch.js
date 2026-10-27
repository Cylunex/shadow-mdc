/**
 * browser-fetch.js — 最后手段：用本机 headless Chrome 渲染一次页面，拿渲染后的 DOM。
 *
 * 有些站点（porndoe / empflix / tube8 / txxx …）的分类索引是前端 JS 渲染出来的，
 * 服务端静态 HTML 里只有语言切换链接，所以链接抽取一无所获。这里在两轮静态尝试
 * 都失败后，调用本机 Chrome 执行一次 JS 再抓 DOM，把分类链接交给同一套抽取逻辑。
 *
 * 约束：
 *   · 只在分类动态解析里当兜底，默认最多 6 次/站，单次 20s 超时；
 *   · 默认不启动无头浏览器（SWIPEFEED_BROWSER_FETCH=1 打开，或本机检测到 Chrome 时也打开）；
 *   · 渲染结果按 URL 缓存 10 分钟。
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';

const CACHE_TTL_MS = 10 * 60 * 1000;
const cache = new Map();
const TIMEOUT_MS = 22000;

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Users\\' + (os.userInfo().username || '') + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/chromium',
  '/usr/bin/google-chrome',
].filter(Boolean);

let chromePath = null; // 未探测
let chromeDisabled = null;

function chromeBin() {
  if (chromeDisabled) return null;
  if (chromePath !== null) return chromePath;
  for (const p of CHROME_CANDIDATES) {
    try {
      if (p && fs.existsSync(p)) {
        chromePath = p;
        return p;
      }
    } catch { /* ignore */ }
  }
  chromePath = '';
  return null;
}

/**
 * 浏览器兜底是否可用：显式开关优先，否则「本机有 Chrome 且没被显式关掉」。
 * SWIPEFEED_BROWSER_FETCH=0 → 关闭；=1 → 强制尝试。
 */
export function browserFetchEnabled() {
  const flag = String(process.env.SWIPEFEED_BROWSER_FETCH ?? '').trim();
  if (flag === '0' || flag === 'false') { chromeDisabled = true; return false; }
  if (flag === '1' || flag === 'true') return true;
  return !!chromeBin();
}

export function browserInfo() {
  const bin = browserFetchEnabled() ? chromeBin() : null;
  return {
    enabled: browserFetchEnabled(),
    path: bin || null,
    engine: bin ? path.basename(bin) : null,
    cachedUrls: cache.size,
  };
}

/** 渲染一个 URL，返回 HTML（失败返回 null）。 */
export async function renderHtml(url, { timeoutMs = TIMEOUT_MS } = {}) {
  const hit = cache.get(url);
  if (hit && Date.now() < hit.at + CACHE_TTL_MS) return hit.html;
  if (!chromeBin()) return null;
  // 走全局并发闸：每次渲染都是一个独立 Chrome 进程
  await acquireSlot();
  try {
    return await new Promise((resolve) => {
      const bin = chromeBin();
      if (!bin) return resolve(null);
      const again = cache.get(url);
      if (again && Date.now() < again.at + CACHE_TTL_MS) return resolve(again.html);

      const profile = path.join(os.tmpdir(), 'swipefeed-chrome-profile');
      const args = [
        '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--disable-extensions', '--disable-background-networking', '--mute-audio',
        `--user-data-dir=${profile}`,
        `--virtual-time-budget=${Math.min(timeoutMs - 2000, 15000)}`,
        '--dump-dom', url,
      ];
      let child;
      let done = false;
      const finish = (html) => {
        if (done) return;
        done = true;
        try { child?.kill(); } catch { /* ignore */ }
        if (html) {
          cache.set(url, { html, at: Date.now() });
          if (cache.size > 60) cache.delete(cache.keys().next().value);
        }
        resolve(html);
      };
      try {
        child = spawn(bin, args, { stdio: ['ignore', 'pipe', 'ignore'] });
      } catch {
        return resolve(null);
      }
    let out = '';
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.on('error', () => finish(null));
    child.on('close', () => finish(out && out.length > 500 ? out : null));
    setTimeout(() => finish(out && out.length > 500 ? out : null), timeoutMs);
    });
  } finally {
    releaseSlot();
  }
}

export function clearBrowserCache() {
  cache.clear();
}

// ——— CDP 抓媒体：渲染 + 记录页面实际请求到的视频地址 ———
//
// 有些站（txxx、beeg）播放器用 MSE，DOM 里只有 blob: URL，静态 HTML 里更没有直链。
// 这里起一个带 remote-debugging 的 headless Chrome，用 CDP 监听 Network 事件，
// 把页面真正请求的 .mp4/.m3u8 记下来 —— 拿到的就是播放器自己认可的地址。

const MEDIA_RE = /\.(?:mp4|m3u8|m4v)(?:\?|$)/i;

// ——— 全局并发闸：每跑一次都是独立 Chrome 进程，同时开太多会把机器拖死 ———
const MAX_CHROME_JOBS = 2;
let activeJobs = 0;
const jobQueue = [];

function acquireSlot() {
  if (activeJobs < MAX_CHROME_JOBS) {
    activeJobs += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => jobQueue.push(resolve));
}

function releaseSlot() {
  activeJobs = Math.max(0, activeJobs - 1);
  const next = jobQueue.shift();
  if (next) {
    activeJobs += 1;
    next();
  }
}

export function browserQueueInfo() {
  return { active: activeJobs, queued: jobQueue.length, max: MAX_CHROME_JOBS };
}

function cdpPort() {
  return 9300 + Math.floor(Math.random() * 500);
}

async function waitJsonList(port, deadline) {
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(1000) });
      const list = await r.json();
      const page = list.find((t) => t.type === 'page');
      if (page?.webSocketDebuggerUrl) return page;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return null;
}

/**
 * 打开一个页面并抓它请求过的媒体地址。
 * @returns {Promise<{media: string[], html: string, finalUrl: string} | null>}
 */
export async function capturePage(url, { timeoutMs = 30000, waitMs = 9000 } = {}) {
  await acquireSlot();
  try {
    return await capturePageInner(url, { timeoutMs, waitMs });
  } finally {
    releaseSlot();
  }
}

async function capturePageInner(url, { timeoutMs, waitMs }) {
  const bin = chromeBin();
  if (!bin) return null;
  const hit = captureCache.get(url);
  if (hit && Date.now() < hit.at + CACHE_TTL_MS) return hit.value;

  const port = cdpPort();
  const profile = path.join(os.tmpdir(), `swipefeed-cdp-${port}`);
  let child;
  try {
    child = spawn(bin, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--disable-extensions', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
      `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
    ], { stdio: 'ignore' });
  } catch {
    return null;
  }

  const cleanup = () => { try { child.kill(); } catch { /* ignore */ } };
  try {
    const page = await waitJsonList(port, Date.now() + 8000);
    if (!page) { cleanup(); return null; }
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('cdp ws timeout')), 5000);
      ws.addEventListener('open', () => { clearTimeout(t); resolve(); }, { once: true });
      ws.addEventListener('error', () => { clearTimeout(t); reject(new Error('cdp ws error')); }, { once: true });
    });

    let msgId = 0;
    const pending = new Map();
    const media = new Set();
    const requests = new Map();
    ws.addEventListener('message', (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
      if (m.method === 'Network.requestWillBeSent') {
        const u = m.params?.request?.url || '';
        requests.set(m.params.requestId, u);
        if (MEDIA_RE.test(u) && /^https?:/i.test(u)) media.add(u);
      }
      if (m.method === 'Network.responseReceived') {
        const u = m.params?.response?.url || '';
        const ct = (m.params?.response?.headers?.['content-type'] || '').toLowerCase();
        if (/^https?:/i.test(u) && (MEDIA_RE.test(u) || /mpegurl|video\//.test(ct))) media.add(u);
      }
    });
    const send = (method, params = {}) => new Promise((resolve) => {
      const id = ++msgId;
      pending.set(id, resolve);
      try { ws.send(JSON.stringify({ id, method, params })); } catch { resolve(null); }
    });

    await send('Page.enable');
    await send('Network.enable');
    await send('Runtime.enable');
    await send('Page.navigate', { url });

    const deadline = Date.now() + Math.min(waitMs, timeoutMs);
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 400));
      if (media.size >= 2) break; // 够用了，不必等满
    }
    let html = '';
    const evalRes = await send('Runtime.evaluate', {
      expression: 'document.documentElement.outerHTML',
      returnByValue: true,
    });
    html = evalRes?.result?.result?.value || '';
    const finalUrl = (await send('Page.getNavigationHistory'))?.result?.currentIndex >= 0
      ? (await send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }))?.result?.result?.value || url
      : url;
    try { ws.close(); } catch { /* ignore */ }

    const value = { media: [...media], html, finalUrl };
    captureCache.set(url, { value, at: Date.now() });
    if (captureCache.size > 40) captureCache.delete(captureCache.keys().next().value);
    return value;
  } catch {
    return null;
  } finally {
    cleanup();
  }
}

const captureCache = new Map();

